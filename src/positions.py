"""Менеджер открытых позиций для exit-логики (гибрид: actor-exit + price TP/SL/trailing/dead).

Стейт открытых позиций персистится в JSON (переживает рестарт контейнера).
Правила выхода — из EXIT_CFG (дефолты по договорённости, потом калибруем).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field

from . import config

EXIT_CFG = {
    "EXIT_ACTOR_FRAC": 0.5,   # вышло >= доли зашедших акторов → exit
    "TP_MULT": 6.0,           # тейк-профит: grid по 522 траекториям — 2.5x резал победителей,
    "SL_MULT": 0.5,           # TP=6 доминирует во всех срезах; рабочий выход = трейлинг
    "TRAIL": 0.35,            # трейлинг: падение от пика на эту долю
    "TRAIL_ARM": 1.5,         # трейлинг включается после роста >= этого × entry
    "DEAD_AGE_H": 1.0,        # нет данных дольше → мёртвый (-100%)
    "MAX_HOLD_S": 1800,       # max-hold таймаут (== replay); дольше не держим (иначе рассинхрон с валидацией)
}


@dataclass
class Position:
    token_mint: str
    entry_ts: float
    entry_price: float
    entry_mc: float | None
    entry_actors: list[str]
    peak_price: float
    exited_actors: list[str] = field(default_factory=list)


class PositionManager:
    def __init__(self, cfg: dict | None = None):
        self.cfg = {**EXIT_CFG, **(cfg or {})}
        self.path = config.OUTPUT_DIR / "open_positions.json"
        self.pos: dict[str, Position] = {}
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                for m, d in json.loads(self.path.read_text(encoding="utf-8")).items():
                    self.pos[m] = Position(**d)
            except Exception:  # noqa: BLE001
                pass

    def _save(self) -> None:
        self.path.write_text(json.dumps({m: asdict(p) for m, p in self.pos.items()},
                                        ensure_ascii=False), encoding="utf-8")

    def open(self, token: str, price: float, mc: float | None, actors: list[str], ts: float) -> bool:
        if token in self.pos or not price or price <= 0:
            return False
        self.pos[token] = Position(token, ts or time.time(), price, mc, list(actors), price)
        self._save()
        return True

    def on_sell(self, token: str, actor: str) -> str | None:
        """Продажа трекаемого токена зашедшим актором. → 'actors_exit' если вышло >= порога."""
        p = self.pos.get(token)
        if not p or actor not in p.entry_actors or actor in p.exited_actors:
            return None
        p.exited_actors.append(actor)
        self._save()
        need = max(1, math.ceil(self.cfg["EXIT_ACTOR_FRAC"] * len(p.entry_actors)))
        if len(p.exited_actors) >= need:
            return "actors_exit"
        return None

    def check_price(self, token: str, cur_price: float | None, age_h: float) -> str | None:
        """Проверка ценовых правил. Обновляет peak. → причина выхода или None."""
        p = self.pos.get(token)
        if not p:
            return None
        # порядок правил == replay: сначала ценовые (TP/SL/trail), потом таймаут, потом dead
        if cur_price is not None:
            if cur_price > p.peak_price:
                p.peak_price = cur_price
                self._save()
            mult = cur_price / p.entry_price if p.entry_price else 0
            if mult >= self.cfg["TP_MULT"]:
                return "take_profit"
            if mult <= self.cfg["SL_MULT"]:
                return "stop_loss"
            peak_mult = p.peak_price / p.entry_price if p.entry_price else 0
            if peak_mult >= self.cfg["TRAIL_ARM"] and cur_price <= p.peak_price * (1 - self.cfg["TRAIL"]):
                return "trailing"
        if age_h * 3600 >= self.cfg["MAX_HOLD_S"]:   # таймаут == replay (не держим дольше валидации)
            return "timeout"
        if cur_price is None:
            return "dead" if age_h > self.cfg["DEAD_AGE_H"] else None
        return None

    def close(self, token: str) -> Position | None:
        p = self.pos.pop(token, None)
        if p is not None:
            self._save()
        return p

    def open_tokens(self) -> list[str]:
        return list(self.pos.keys())

    def get(self, token: str) -> Position | None:
        return self.pos.get(token)


if __name__ == "__main__":   # самотест логики выходов
    import os
    f = config.OUTPUT_DIR / "open_positions.json"
    if f.exists():
        os.remove(f)
    pm = PositionManager()
    print("open:", pm.open("TOK", 0.001, 1_000_000, ["a1", "a2", "a3"], 1000))
    print("sell a1 (1/3 < 50%):", pm.on_sell("TOK", "a1"))
    print("sell a2 (2/3 >= 50%):", pm.on_sell("TOK", "a2"))
    print("reload persist:", PositionManager().open_tokens())
    print("TP (3x):", pm.check_price("TOK", 0.003, 0.1))
    print("SL (0.4x):", pm.check_price("TOK", 0.0004, 0.1))
    pm.check_price("TOK", 0.002, 0.1)                      # взводим peak на 2x
    print("trailing (0.0012 vs peak 0.002):", pm.check_price("TOK", 0.0012, 0.1))
    print("timeout (age 2ч > max-hold 0.5ч):", pm.check_price("TOK", None, 2.0))
    if f.exists():
        os.remove(f)
