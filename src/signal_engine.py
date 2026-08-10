"""Модуль G — движок сигнала (конфлюенс). Чистая логика, тестируется синтетикой (без Helius).

Вход: поток BuyEvent (watchlist-кошелёк купил токен). Движок держит per-токен скользящее окно
покупок, маппит кошельки в АКТОРОВ (флоу-watchlist) и эмитит Signal, когда ≥CONFLUENCE_N разных
акторов сошлись на одном раннем токене за окно. Дедуп: эмит при первом пороге, апгрейд при росте.

velocity (всего покупателей) и safety — ENRICHMENT на стороне монитора (Helius/RugCheck), не здесь.
"""
from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from . import config, strategy

# Параметры входа — из ЕДИНОГО источника config/strategy.yaml (аудит-4: были разбросаны).
DEFAULTS = dict(strategy.SIGNAL)


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
    # ПОРЯДОК ЗНАЧИМ: акторы идут в порядке их ПЕРВОЙ покупки в окне, поэтому
    # actors[0] — тот, кто зашёл раньше всех. Гарантия держится на двух вещах:
    # st["buys"] хронологичен (append в конец, popleft при истечении окна), а dict
    # сохраняет порядок вставки. Ротация watchlist опирается на это: при исключении
    # актора надо знать, потеряем ли мы сигнал совсем или войдём позже (аудит 09.08).
    actors: list[str]
    n_actors: int
    window_usd: float
    strength: float
    level: str                   # 'weak' | 'strong'
    first_buy_ts: float
    quiet: bool = False          # window_usd < QUIET_MAX_USD — «тихий» = лучший класс (см. ресерч)
    first_gap_s: float = 0.0     # сек от первой покупки в окне до схождения (формирование конфлюенса)
    n_buys: int = 0              # всего покупок в окне (для будущих OOS: DCA vs уник)
    actor_first_usd: float = 0.0  # сумма ПЕРВЫХ покупок по актору (без DCA) — для будущей замены метрики


def load_actor_map(path: Path | None = None) -> dict[str, tuple[str, float]]:
    """wallet -> (actor_id, weight) из флоу-watchlist."""
    p = path or (config.OUTPUT_DIR / "flow_watchlist.json")
    data = json.loads(Path(p).read_text(encoding="utf-8"))
    m: dict[str, tuple[str, float]] = {}
    for a in data:
        for w in a["wallets"]:
            m[w] = (a["actor_id"], float(a["weight"]))
    return m


# Уборка состояния токенов (аудит 10.08). Словарь self.tokens рос без ограничений: запись
# заводилась на КАЖДЫЙ увиденный токен и не удалялась никогда, а окно покупок подрезалось
# только когда по этому токену приходило новое событие. Мёртвый токен уносил свой список
# покупок навсегда. Замер по памяти контейнера: 272.6 -> 273.2 МиБ за 24 минуты, около
# 36 МБ в сутки (две точки, оценка грубая; неограниченность структуры при этом — факт).
PRUNE_EVERY = 500        # как часто убирать: раз в N событий, чтобы не платить на каждом
MAX_TOKENS = 50_000      # потолок числа токенов в памяти; сверх него вытесняем самые старые


class SignalEngine:
    def __init__(self, actor_map: dict[str, tuple[str, float]], cfg: dict | None = None):
        self.actor_map = actor_map
        self.cfg = {**DEFAULTS, **(cfg or {})}
        self.tokens: dict[str, dict] = {}
        self._since_prune = 0

    def _prune(self, now: float) -> int:
        """Освободить память, НЕ меняя решений движка. → сколько записей затронуто.

        Две ступени, и первая важнее второй:

        1. У токенов, чьё окно уже истекло, очищаем список покупок. Поведение при этом
           не меняется ни на йоту: приди по такому токену событие, цикл popleft всё
           равно выбросил бы эти покупки первым же делом. Память они занимают, а решают
           ноль. Счётчик last_n СОХРАНЯЕМ: он не даёт повторно сигналить на том же
           уровне конфлюенса, и его сброс был бы уже изменением правила входа.

        2. Если токенов всё равно больше потолка — вытесняем самые старые целиком.
           Тут last_n теряется, поэтому ступень стоит второй и срабатывает только на
           токенах, по которым давно ничего не происходило.
        """
        win = self.cfg["CONFLUENCE_WINDOW_S"]
        touched = 0
        for st in self.tokens.values():
            if st["buys"] and now - st["buys"][-1][0] > win:
                st["buys"].clear()
                touched += 1
        if len(self.tokens) > MAX_TOKENS:
            # dict сохраняет порядок вставки → первые ключи и есть самые старые
            for m in list(self.tokens)[:len(self.tokens) - MAX_TOKENS]:
                del self.tokens[m]
                touched += 1
        return touched

    def process(self, ev: BuyEvent) -> Signal | None:
        info = self.actor_map.get(ev.wallet)
        if not info or ev.usd < self.cfg["SIGNAL_MIN_USD"]:
            return None
        self._since_prune += 1
        if self._since_prune >= PRUNE_EVERY:
            self._since_prune = 0
            self._prune(ev.ts)
        # ПРИМЕЧАНИЕ (аудит-6): гейты по MC/возрасту УДАЛЕНЫ. Они никогда не срабатывали
        # (BuyEvent создаётся без token_mc), а замер показал, что включать их ВРЕДНО:
        # сигналы с MC>100k — лучшая когорта (win 0.57 против 0.42 в среднем).
        # Реальный потолок задаёт monitor --max-mc. Мёртвый код, похожий на защиту, опасен.

        actor_id, weight = info
        st = self.tokens.setdefault(ev.token_mint, {"buys": deque(), "last_n": 0})
        st["buys"].append((ev.ts, actor_id, weight, ev.usd))
        win = self.cfg["CONFLUENCE_WINDOW_S"]
        while st["buys"] and ev.ts - st["buys"][0][0] > win:
            st["buys"].popleft()

        # разные акторы в окне
        actors: dict[str, float] = {}
        actor_first: dict[str, float] = {}   # первая покупка по актору (без DCA-дублей)
        total_usd = 0.0
        for _ts, aid, wt, usd in st["buys"]:
            actors[aid] = max(actors.get(aid, 0.0), wt)
            if aid not in actor_first:
                actor_first[aid] = usd
            total_usd += usd
        n = len(actors)
        if n < self.cfg["CONFLUENCE_N"] or n <= st["last_n"]:
            return None                       # порог не достигнут ИЛИ уже сигналили на этом уровне
        st["last_n"] = n

        # strength = конвикция акторов + бонус за ТИШИНУ (низкий объём = лучший класс по OOS).
        # Прежний '+ total_usd/1000' был АНТИ-предиктивен (награждал громкие = убыточные сигналы,
        # громкие OOS mean −6.5% vs тихие +28%) — заменён на quiet-бонус.
        total_usd_r = round(total_usd)
        weight_sum = sum(actors.values())
        quiet_bonus = max(0.0, self.cfg["QUIET_MAX_USD"] - total_usd_r) / 100.0
        strength = round(weight_sum + quiet_bonus, 2)
        level = "strong" if n >= self.cfg["STRONG_CONFLUENCE_N"] else "weak"
        return Signal(token_mint=ev.token_mint, ts=ev.ts, actors=list(actors), n_actors=n,
                      window_usd=total_usd_r, strength=strength, level=level,
                      first_buy_ts=st["buys"][0][0],
                      quiet=total_usd_r < self.cfg["QUIET_MAX_USD"],
                      first_gap_s=round(ev.ts - st["buys"][0][0], 1),
                      n_buys=len(st["buys"]),
                      actor_first_usd=round(sum(actor_first.values())))


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
