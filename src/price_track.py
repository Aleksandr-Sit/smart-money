"""Трекер ценовых траекторий сигнальных токенов → output/price_history.jsonl.

Зачем: PAPER v3 «режим потребителя». Чтобы честно оценить вход с задержкой (+5с бот,
+60с человек) и офлайн-калибровать EXIT_CFG, нужна траектория цены КАЖДОГО сигнального
токена независимо от жизни paper-позиции.

Как: цена ранних pump.fun токенов читается ПРЯМО с бондинг-кривой (PDA от mint,
virtual reserves) — DexScreener их не индексирует. Все активные токены опрашиваются
ОДНИМ getMultipleAccounts (1 кредит/тик на Helius, не 1/токен). После грэдуэйшна
(complete=true) — фолбэк на DexScreener.

Бюджет: тик 15с только при наличии активных токенов; окно трекинга 45 мин/токен.

Self-test (реальный mint):  .venv\\Scripts\\python.exe -m src.price_track <mint>
"""
from __future__ import annotations

import asyncio
import base64
import json
import struct
import time
from hashlib import sha256

from . import config, helius, market, strategy

PUMP_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
# Единый конфиг (аудит-4). TICK_S — не деталь, а ЧАСТЬ СТРАТЕГИИ: на 90с edge исчезал.
TICK_S = strategy.TRACKING["TICK_S"]
TRACK_S = strategy.TRACKING["TRACK_S"]
HISTORY = "price_history.jsonl"

# ---------- base58 (pure python, без зависимостей) ----------
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_B58_IDX = {c: i for i, c in enumerate(_B58)}


def b58decode(s: str) -> bytes:
    n = 0
    for c in s:
        n = n * 58 + _B58_IDX[c]
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


# ---------- ed25519 off-curve проверка (для PDA) ----------
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def _on_curve(pt: bytes) -> bool:
    """Есть ли у 32 байт валидная декомпрессия в точку ed25519 (RFC 8032)."""
    y = int.from_bytes(pt, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    y2 = y * y % _P
    u, v = (y2 - 1) % _P, (_D * y2 + 1) % _P
    if v == 0:
        return False
    xx = u * pow(v, _P - 2, _P) % _P
    return xx == 0 or pow(xx, (_P - 1) // 2, _P) == 1


def bonding_curve_pda(mint: str) -> str:
    """PDA бондинг-кривой pump.fun: seeds=['bonding-curve', mint], bump 255→0, off-curve."""
    mint_b, prog_b = b58decode(mint), b58decode(PUMP_PROGRAM)
    for bump in range(255, -1, -1):
        h = sha256(b"bonding-curve" + mint_b + bytes([bump]) + prog_b
                   + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise ValueError("no PDA bump found")  # практически недостижимо


def parse_curve(data: bytes) -> dict | None:
    """Аккаунт кривой: disc(8) + virt_tok u64 + virt_sol u64 + real_tok + real_sol + supply + complete."""
    if len(data) < 49:
        return None
    vt, vs, rt, rs, sup, complete = struct.unpack_from("<QQQQQ?", data, 8)
    if not vt:
        return None
    return {"price_sol": (vs / 1e9) / (vt / 1e6), "complete": complete,
            "virt_sol": vs / 1e9, "virt_tok": vt / 1e6}


# ---------- трекер ----------
class PriceTracker:
    """register(mint, price0) при сигнале; run() — фоновый цикл, пишет price_history.jsonl."""

    SANITY_JUMP = strategy.ALERTS["SANITY_JUMP"]   # скачок больше этого = мусор источника (аудит-4)

    def __init__(self):
        self.active: dict[str, dict] = {}   # mint -> {pda, t0}
        self.last: dict[str, float] = {}    # последняя валидная цена (санитарный фильтр)
        self.anomalies = 0                  # счётчик отбитых аномалий → алерт качества данных
        self.rpc_fails = 0
        self.path = config.OUTPUT_DIR / HISTORY

    def _append(self, row: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def register(self, mint: str, price0: float | None, ts: float | None = None) -> None:
        if mint in self.active:
            return
        t0 = ts or time.time()
        self.active[mint] = {"pda": bonding_curve_pda(mint), "t0": t0}
        if price0:                            # якорь t0 — цена он-чейн покупки из сигнала
            self._append({"ts": t0, "mint": mint, "price_usd": price0, "src": "signal"})

    def _poll_once(self) -> list[dict]:
        """Синхронный опрос (зовётся из executor): один батч по всем активным."""
        now = time.time()
        for m in [m for m, st in self.active.items() if now - st["t0"] > TRACK_S]:
            del self.active[m]
        if not self.active:
            return []
        mints = list(self.active)
        rows: list[dict] = []
        sol = market.sol_price()
        for i in range(0, len(mints), 100):   # лимит getMultipleAccounts = 100
            chunk = mints[i:i + 100]
            try:
                res = helius.rpc("getMultipleAccounts",
                                 [[self.active[m]["pda"] for m in chunk],
                                  {"encoding": "base64"}]).get("result", {}).get("value") or []
            except Exception as e:  # noqa: BLE001
                self.rpc_fails += 1
                print(f"[track] rpc fail: {type(e).__name__}")
                continue
            for m, acc in zip(chunk, res):
                row = None
                curve = None
                if acc and acc.get("data"):
                    curve = parse_curve(base64.b64decode(acc["data"][0]))
                if curve and not curve["complete"]:
                    row = {"ts": now, "mint": m, "price_usd": curve["price_sol"] * sol, "src": "curve"}
                else:                          # грэдуэйшн/нет кривой → DexScreener
                    info = market.token_info(m)
                    if info.get("price_usd"):
                        row = {"ts": now, "mint": m, "price_usd": info["price_usd"], "src": "dex"}
                if row:
                    # санитарный фильтр: невозможный скачок (баг источника, см. аудит-4 —
                    # DexScreener отдавал цену чужого токена → фиктивные +321645%)
                    prev = self.last.get(m)
                    if prev and prev > 0:
                        ratio = row["price_usd"] / prev
                        if ratio > self.SANITY_JUMP or ratio < 1 / self.SANITY_JUMP:
                            self.anomalies += 1
                            print(f"[track] SKIP аномалия {m[:8]}: {prev:.3e}->{row['price_usd']:.3e} "
                                  f"({ratio:.0f}x, src={row['src']})")
                            continue
                    self.last[m] = row["price_usd"]
                    rows.append(row)
        for r in rows:
            self._append(r)
        return rows

    async def run(self, on_tick=None) -> None:
        """Каждые TICK_S поллит цены. on_tick(prices: dict[mint,price]) — драйвер выходов на 15с
        (вместо отдельной медленной петли): даёт exit-гранулярность == валидированной."""
        loop = asyncio.get_event_loop()
        while True:
            await asyncio.sleep(TICK_S)
            try:
                rows = await loop.run_in_executor(None, self._poll_once)
                if on_tick and rows is not None:
                    await on_tick({r["mint"]: r["price_usd"] for r in rows})
            except Exception as e:  # noqa: BLE001
                print(f"[track] loop error: {type(e).__name__}: {e}")


# ---------- self-test на реальном mint ----------
def _selftest(mints: list[str]) -> None:
    sol = market.sol_price()
    print(f"SOL=${sol:.2f}")
    for mint in mints:
        pda = bonding_curve_pda(mint)
        acc = helius.rpc("getAccountInfo", [pda, {"encoding": "base64"}]).get(
            "result", {}).get("value")
        if not acc:
            print(f"{mint[:12]}… pda={pda[:12]}… НЕТ АККАУНТА (не pump.fun токен?)")
            continue
        owner_ok = acc.get("owner") == PUMP_PROGRAM
        curve = parse_curve(base64.b64decode(acc["data"][0]))
        if curve:
            print(f"{mint[:12]}… pda_owner_ok={owner_ok} complete={curve['complete']} "
                  f"price=${curve['price_sol'] * sol:.8f} MC=${curve['price_sol'] * sol * 1e9:,.0f} "
                  f"(virt_sol={curve['virt_sol']:.2f})")
        else:
            print(f"{mint[:12]}… pda_owner_ok={owner_ok} — не распарсили layout")


if __name__ == "__main__":
    import sys
    _selftest(sys.argv[1:] or ["6Cn9XH2SCi79Nam7mhH5L1kfwFwPhfresmg6Q4Eipump"])
