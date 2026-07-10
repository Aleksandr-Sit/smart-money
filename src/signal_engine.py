"""Модуль G — движок сигнала (конфлюенс). Чистая логика, тестируется синтетикой (без Helius).

Вход: поток BuyEvent (watchlist-кошелёк купил токен). Движок держит per-токен скользящее окно
покупок, маппит кошельки в АКТОРОВ (флоу-watchlist) и эмитит Signal, когда ≥CONFLUENCE_N разных
акторов сошлись на одном раннем токене за окно. Дедуп: эмит при первом пороге, апгрейд при росте.

velocity (всего покупателей) и safety — ENRICHMENT на стороне монитора (Helius/RugCheck), не здесь.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from . import config

DEFAULTS = {
    "CONFLUENCE_N": 2,          # разных акторов для сигнала
    "STRONG_CONFLUENCE_N": 3,   # порог strong
    "CONFLUENCE_WINDOW_S": 600,  # окно схождения, сек
    "SIGNAL_MAX_MC_USD": 100_000,   # токен ещё ранний
    "SIGNAL_MAX_AGE_S": 3600,       # возраст токена <= 1ч
    "SIGNAL_MIN_USD": 20,           # не пыль
}


@dataclass
class BuyEvent:
    ts: float                    # epoch сек
    token_mint: str
    wallet: str
    usd: float = 0.0
    token_mc: float | None = None
    token_age_s: float | None = None


@dataclass
class Signal:
    token_mint: str
    ts: float
    actors: list[str]
    n_actors: int
    window_usd: float
    strength: float
    level: str                   # 'weak' | 'strong'
    first_buy_ts: float


def load_actor_map(path: Path | None = None) -> dict[str, tuple[str, float]]:
    """wallet -> (actor_id, weight) из флоу-watchlist."""
    p = path or (config.OUTPUT_DIR / "flow_watchlist.json")
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    m: dict[str, tuple[str, float]] = {}
    for a in data:
        for w in a["wallets"]:
            m[w] = (a["actor_id"], float(a["weight"]))
    return m


class SignalEngine:
    def __init__(self, actor_map: dict[str, tuple[str, float]], cfg: dict | None = None):
        self.actor_map = actor_map
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.tokens: dict[str, dict] = {}

    def process(self, ev: BuyEvent) -> Signal | None:
        info = self.actor_map.get(ev.wallet)
        if not info or ev.usd < self.cfg["SIGNAL_MIN_USD"]:
            return None
        # ранний ли токен (если известно)
        if ev.token_mc is not None and ev.token_mc > self.cfg["SIGNAL_MAX_MC_USD"]:
            return None
        if ev.token_age_s is not None and ev.token_age_s > self.cfg["SIGNAL_MAX_AGE_S"]:
            return None

        actor_id, weight = info
        st = self.tokens.setdefault(ev.token_mint, {"buys": deque(), "last_n": 0})
        st["buys"].append((ev.ts, actor_id, weight, ev.usd))
        win = self.cfg["CONFLUENCE_WINDOW_S"]
        while st["buys"] and ev.ts - st["buys"][0][0] > win:
            st["buys"].popleft()

        # разные акторы в окне
        actors: dict[str, float] = {}
        total_usd = 0.0
        for _ts, aid, wt, usd in st["buys"]:
            actors[aid] = max(actors.get(aid, 0.0), wt)
            total_usd += usd
        n = len(actors)
        if n < self.cfg["CONFLUENCE_N"] or n <= st["last_n"]:
            return None                       # порог не достигнут ИЛИ уже сигналили на этом уровне
        st["last_n"] = n

        strength = round(sum(actors.values()) + total_usd / 1000.0, 2)
        level = "strong" if n >= self.cfg["STRONG_CONFLUENCE_N"] else "weak"
        return Signal(token_mint=ev.token_mint, ts=ev.ts, actors=list(actors), n_actors=n,
                      window_usd=round(total_usd), strength=strength, level=level,
                      first_buy_ts=st["buys"][0][0])


# ---------- синтетический self-test ----------
def _demo() -> None:
    amap = {
        "walA": ("actor1", 10.0), "walA2": ("actor1", 10.0),  # actor1 (2 кошелька, ротация)
        "walB": ("actor2", 8.0),
        "walC": ("actor3", 5.0),
        "walD": ("actor4", 3.0),
    }
    eng = SignalEngine(amap, {"CONFLUENCE_N": 2, "STRONG_CONFLUENCE_N": 3, "CONFLUENCE_WINDOW_S": 600})
    tok = "TokenMintXYZ"
    events = [
        BuyEvent(1000, tok, "walA", usd=150, token_mc=30_000, token_age_s=120),   # actor1
        BuyEvent(1005, tok, "walA2", usd=90, token_mc=32_000, token_age_s=125),   # actor1 снова (не новый актор)
        BuyEvent(1060, tok, "walB", usd=200, token_mc=45_000, token_age_s=180),   # actor2 -> N=2 weak
        BuyEvent(1120, tok, "walC", usd=300, token_mc=60_000, token_age_s=240),   # actor3 -> N=3 strong
        BuyEvent(1130, tok, "walD", usd=5, token_mc=61_000, token_age_s=250),     # пыль <MIN -> ignore
        BuyEvent(9000, tok, "walD", usd=500, token_mc=90_000, token_age_s=8000),  # поздно/вне окна -> нет апгрейда
        BuyEvent(1200, "OtherTok", "walZ", usd=100),                              # не watchlist -> ignore
    ]
    print("Синтетический тест движка сигнала:")
    for ev in events:
        sig = eng.process(ev)
        if sig:
            print(f"  SIGNAL[{sig.level}] {sig.token_mint} n_actors={sig.n_actors} "
                  f"usd={sig.window_usd} strength={sig.strength} actors={sig.actors}")
        else:
            print(f"  (нет сигнала на ev wallet={ev.wallet} usd={ev.usd})")


if __name__ == "__main__":
    _demo()
