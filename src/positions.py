"""Менеджер открытых позиций для exit-логики (гибрид: actor-exit + price TP/SL/trailing/dead).

Стейт открытых позиций персистится в JSON (переживает рестарт контейнера).
Правила выхода — из EXIT_CFG (дефолты по договорённости, потом калибруем).
"""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict, dataclass, field

from . import config, strategy

# Правила выхода — из ЕДИНОГО источника config/strategy.yaml (аудит-4).
EXIT_CFG = dict(strategy.EXIT)


@dataclass
class Position:
    token_mint: str
    entry_ts: float
    entry_price: float
    entry_mc: float | None
    entry_actors: list[str]
    peak_price: float
    exited_actors: list[str] = field(default_factory=list)
    remaining: float = 1.0                        # непроданная доля позиции
    realized: float = 0.0                         # накопл. реализ. вклад = сумма frac*(mult-1)
    taken: list[int] = field(default_factory=list)  # индексы взятых частичных тейков


def total_realized(p: Position, exit_price: float | None) -> float:
    """Итоговый PnL позиции: уже реализованное + остаток по цене выхода (dead/None = -остаток)."""
    if exit_price and p.entry_price:
        tail = p.remaining * (exit_price / p.entry_price - 1)
    else:
        tail = p.remaining * -1.0
    return p.realized + tail


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

    def check_price(self, token: str, cur_price: float | None, age_h: float) -> dict | None:
        """Проверка ценовых правил с ЧАСТИЧНЫМИ тейками. Обновляет peak/remaining/realized.

        → None | {"action":"partial","reason":...,"frac":x} (позиция продолжается с меньшим остатком)
              | {"action":"close","reason":...}             (закрыть остаток).
        Порядок как в replay: частичные тейки → TP/SL/trail остатка → таймаут → dead.
        """
        p = self.pos.get(token)
        if not p:
            return None
        if cur_price is not None:
            if cur_price > p.peak_price:
                p.peak_price = cur_price
                self._save()
            mult = cur_price / p.entry_price if p.entry_price else 0
            # частичные тейки: продать долю на достигнутом уровне (позиция продолжается)
            for i, (tm, fr) in enumerate(self.cfg["PARTIAL_TAKES"]):
                if i not in p.taken and mult >= tm and p.remaining > 1e-9:
                    sell = min(fr, p.remaining)
                    p.realized += sell * (mult - 1)
                    p.remaining -= sell
                    p.taken.append(i)
                    self._save()
                    if p.remaining <= 1e-9:
                        return {"action": "close", "reason": "take_profit"}
                    return {"action": "partial", "reason": "take_partial", "frac": sell}
            if mult >= self.cfg["TP_MULT"]:
                return {"action": "close", "reason": "take_profit"}
            if mult <= self.cfg["SL_MULT"]:
                return {"action": "close", "reason": "stop_loss"}
            peak_mult = p.peak_price / p.entry_price if p.entry_price else 0
            if peak_mult >= self.cfg["TRAIL_ARM"] and cur_price <= p.peak_price * (1 - self.cfg["TRAIL"]):
                return {"action": "close", "reason": "trailing"}
        if age_h * 3600 >= self.cfg["MAX_HOLD_S"]:
            return {"action": "close", "reason": "timeout"}
        if cur_price is None:
            return {"action": "close", "reason": "dead"} if age_h > self.cfg["DEAD_AGE_H"] else None
        return None

    def rollback_partial(self, token: str, frac: float, mult: float) -> None:
        """Отменить учтённый частичный тейк: в live продажа могла не пройти.

        check_price уменьшает remaining СРАЗУ, до отправки сделки. Если своп упал,
        токены остались в кошельке — и без отката бот считал бы проданным то, чем
        всё ещё владеет, а на выходе продал бы меньше, чем нужно.
        """
        p = self.pos.get(token)
        if not p:
            return
        p.realized -= frac * (mult - 1)
        p.remaining += frac
        if p.taken:
            p.taken.pop()
        self._save()

    def close(self, token: str) -> Position | None:
        p = self.pos.pop(token, None)
        if p is not None:
            self._save()
        return p

    def open_tokens(self) -> list[str]:
        return list(self.pos.keys())

    def get(self, token: str) -> Position | None:
        return self.pos.get(token)


if __name__ == "__main__":   # самотест логики выходов (частичные тейки)
    import os
    f = config.OUTPUT_DIR / "open_positions.json"
    if f.exists():
        os.remove(f)
    pm = PositionManager()
    pm.open("TOK", 0.001, 1_000_000, ["a1", "a2"], 1000)    # entry 0.001
    print("partial 50%@2x (цена 0.002):", pm.check_price("TOK", 0.002, 0.1))
    pos = pm.get("TOK")
    print(f"  → remaining={pos.remaining} realized={pos.realized} (ожид 0.5 / 0.5)")
    print("нет 2-го тейка ниже 2x (0.0018):", pm.check_price("TOK", 0.0018, 0.1))
    print("close остатка по TP6 (0.006):", pm.check_price("TOK", 0.006, 0.1))
    print(f"  total_realized при выходе 0.006: {total_realized(pos, 0.006):+.2f} (ожид 0.5+0.5*5=3.0)")

    if f.exists():
        os.remove(f)
    pm = PositionManager()
    pm.open("T2", 0.001, 1e6, ["a1", "a2"], 1000)
    print("\nSL остатка (0.0004) без тейка:", pm.check_price("T2", 0.0004, 0.1))
    print(f"  total_realized при SL 0.0004: {total_realized(pm.get('T2'), 0.0004):+.2f} (ожид -0.6)")
    print("actor-exit a1 (1/2>=50%):", pm.on_sell("T2", "a1"))
    print("timeout без цены age2ч:", pm.check_price("T2", None, 2.0))
    if f.exists():
        os.remove(f)
