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


def _events(logs: list[str]) -> list[dict]:
    """Все anchor-события сделок в логе. Их может быть несколько: агрегаторы и
    бандлы кладут в одну транзакцию сделки разных участников и разных токенов."""
    out = []
    for line in logs:
        if not line.startswith("Program data:"):
            continue
        try:
            raw = base64.b64decode(line.split("Program data: ", 1)[1])
        except Exception:  # noqa: BLE001
            continue
        if len(raw) < 89:                      # нужен и хвост с полем user
            continue
        try:
            mint = b58encode(raw[8:40])
            sol_lamports, token_raw = struct.unpack_from("<QQ", raw, 40)
            is_buy = bool(raw[56])
            user = b58encode(raw[57:89])
        except Exception:  # noqa: BLE001
            continue
        if not sol_lamports or not token_raw:
            continue
        e = {"mint": mint, "user": user, "lamports": sol_lamports,
             "tokens": token_raw, "is_buy": is_buy, "curve_price_sol": None}
        # ВИРТУАЛЬНЫЕ РЕЗЕРВЫ ПОСЛЕ СДЕЛКИ (найдено 12.08). За полем user в TradeEvent
        # идут timestamp(i64) и virtual_sol_reserves/virtual_token_reserves (u64).
        # Это цена кривой в МОМЕНТ сделки, полученная даром: событие и так приходит
        # к нам по WS. Раньше за той же ценой ходили отдельным getAccountInfo, и
        # ответ приходил секундами позже — а на свежем токене это уже другая цена.
        # Проверка на живой транзакции: цена кривой после покупки = 1.0019 от цены
        # исполнения, ровно как предписывает математика бондинг-кривой.
        if len(raw) >= 113:
            try:
                _ts, vsol, vtok = struct.unpack_from("<qQQ", raw, 89)
                if vsol > 0 and vtok > 0:
                    e["curve_price_sol"] = (vsol / 1e9) / (vtok / 1e6)
            except Exception:  # noqa: BLE001
                pass
        out.append(e)
    return out


def parse_logs(logs: list[str], signature: str = "", wallet: str = "") -> dict | None:
    """Логи события → {side, token_mint, base_amount, sol, ts, source} или None.

    ОБЯЗАТЕЛЬНО передавать wallet. Подписка `mentions` срабатывает на ЛЮБОЕ упоминание
    адреса в транзакции — в том числе когда наш актор просто указан рефералом или
    сосчитан в чужом бандле. Замер 07.08 на живом потоке: 44% событий содержали сделку
    ЧУЖОГО кошелька. Без сверки поля `user` мы приписывали актору чужие покупки и
    строили на них сигналы — win упал с 0.49 до 0.35.

    Старый tx_parse такой ошибки не делал: он требовал `wallet in accountKeys` и считал
    изменение баланса именно этого кошелька. Здесь тот же инвариант, но по полю user.

    None означает «в логах нет сделки НАШЕГО кошелька» — вызывающий может сходить за
    getTransaction, который разберёт транзакцию авторитетно, по балансам.
    """
    if not logs or not is_trade(logs):
        return None
    evs = _events(logs)
    if not evs:
        return None
    if not wallet:
        return None                            # без адреса сверить принадлежность нечем
    mine = [e for e in evs if e["user"] == wallet]
    if not mine:
        return None
    # если наш кошелёк торговал несколько раз в одной транзакции — берём крупнейшую по SOL
    e = max(mine, key=lambda x: x["lamports"])
    return {
        "side": "buy" if e["is_buy"] else "sell",
        "token_mint": e["mint"],
        "base_amount": e["tokens"] / 1e6,        # pump.fun: 6 знаков
        "sol": e["lamports"] / 1e9,
        "usd_proceeds": 0.0,
        "fee": 0.0,                              # комиссия в логах не выделена; учтена в EXIT_FEE
        # цена кривой в момент сделки — из того же события, без единого запроса
        "curve_price_sol": e.get("curve_price_sol"),
        "ts": None,                              # проставит вызывающий (время получения)
        "source": "logs",
        "signature": signature,
    }


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
