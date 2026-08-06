"""Разбор сделки ПРЯМО ИЗ ЛОГОВ WebSocket — без getTransaction.

ЗАЧЕМ (инцидент 06.08): мы вызывали getTransaction на КАЖДОЕ событие от 148 кошельков —
242 240 кредитов в сутки, 97% всего расхода, ~646 запросов на один полезный сигнал.
Миллион кредитов Helius сгорел за 4 дня. При этом данные о сделке УЖЕ приходили в
push-уведомлении, в поле logs — мы их игнорировали и платили за то же самое второй раз.

ФОРМАТ (проверен на живой сделке 06.08): pump.fun эмитит anchor-событие в строке
`Program data: <base64>`; после 8-байтового дискриминатора идёт
  mint      32 байта  [8:40]
  solAmount u64       [40:48]
  tokenAmount u64     [48:56]
  isBuy     bool      [56]
  user      32 байта  [57:89]
Декодировано вживую: SOL=0.005034, токенов=171139, isBuy=False при `Instruction: Sell`.

Экономия: 250 000 → 7 760 кредитов в сутки (запас 4.3x к лимиту 1M/мес) И независимость
от провайдера — те же логи бесплатно отдают публичные узлы.
"""
from __future__ import annotations

import base64
import struct

PUMP = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def is_trade(logs: list[str]) -> bool:
    """Быстрый гейт: похоже ли событие на сделку pump.fun. Дешевле полного разбора."""
    txt = "\n".join(logs)
    if PUMP not in txt and PUMP_AMM not in txt:
        return False
    return "Instruction: Buy" in txt or "Instruction: Sell" in txt


def parse_logs(logs: list[str], signature: str = "") -> dict | None:
    """Логи события → {side, token_mint, base_amount, sol, ts, source} или None.

    Формат ответа совместим с tx_parse.parse_trade, чтобы монитор не переписывать.
    None означает «это не разбираемая сделка» — тогда вызывающий может (по желанию)
    сходить за getTransaction, но в норме этого не требуется.
    """
    if not logs or not is_trade(logs):
        return None
    for line in logs:
        if not line.startswith("Program data:"):
            continue
        try:
            raw = base64.b64decode(line.split("Program data: ", 1)[1])
        except Exception:  # noqa: BLE001
            continue
        if len(raw) < 57:
            continue
        try:
            mint = b58encode(raw[8:40])
            sol_lamports, token_raw = struct.unpack_from("<QQ", raw, 40)
            is_buy = bool(raw[56])
        except Exception:  # noqa: BLE001
            continue
        if not sol_lamports or not token_raw:
            continue
        return {
            "side": "buy" if is_buy else "sell",
            "token_mint": mint,
            "base_amount": token_raw / 1e6,      # pump.fun: 6 знаков
            "sol": sol_lamports / 1e9,
            "usd_proceeds": 0.0,
            "fee": 0.0,                          # комиссия в логах не выделена; учтена в EXIT_FEE
            "ts": None,                          # проставит вызывающий (время получения)
            "source": "logs",
            "signature": signature,
        }
    return None


if __name__ == "__main__":     # живой самотест на публичном RPC (без ключей и без кредитов)
    import asyncio
    import json
    import websockets

    async def main() -> None:
        url = "wss://api.mainnet-beta.solana.com"
        async with websockets.connect(url, open_timeout=20) as ws:
            await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "logsSubscribe",
                                      "params": [{"mentions": [PUMP]}, {"commitment": "confirmed"}]}))
            await ws.recv()
            found = 0
            while found < 3:
                m = json.loads(await asyncio.wait_for(ws.recv(), timeout=30))
                if m.get("method") != "logsNotification":
                    continue
                v = m["params"]["result"]["value"]
                if v.get("err"):
                    continue
                t = parse_logs(v.get("logs") or [], v.get("signature", ""))
                if t:
                    found += 1
                    print(f"{t['side']:<4} {t['token_mint'][:14]}… "
                          f"SOL={t['sol']:.6f} токенов={t['base_amount']:,.0f}")
            print("разбор из логов работает — getTransaction не требуется")

    asyncio.run(main())
