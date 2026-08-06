"""Async WS-клиент Helius: logsSubscribe(mentions) по батчу кошельков, callback на каждую tx.

Solana ограничение: mentions = ровно один адрес → одна подписка на кошелёк.
Free Helius: до 5 одновременных соединений; распределяем кошельки по ним.

Надёжность: экспоненциальный backoff с джиттером при обрыве + BACKFILL пропущенных
сигнатур через getSignaturesForAddress (окно разрыва не теряет продажи акторов).
Дедуп на стороне on_event снимает пересечение backfill с live-потоком.
"""
from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable

import websockets

from . import helius

BACKOFF_BASE_S = 3
BACKOFF_MAX_S = 600          # было 60 — инцидент 06.08: 5 соединений × попытка/мин = 242 попытки/час,
                             # Helius ответил HTTP 429 и заблокировал ДАЖЕ одиночное подключение.
                             # Агрессивный ретрай сам поддерживал блокировку.
BACKOFF_JITTER_S = 45        # было 2 — все 5 соединений переподключались СИНХРОННО (60.3/61.7/60.3с),
                             # это «стадо»: пять одновременных попыток вместо равномерных
RATE_LIMIT_PENALTY_S = 900   # после 429 ждём отдельно и долго: сервер явно просит перестать
BACKFILL_LIMIT = 50          # максимум сигнатур на кошелёк за один backfill


async def _backfill(wallets: list[str], last_sig: dict[str, str],
                    on_event: Callable[[str, str], Awaitable[None]], label: str) -> None:
    """Догрузить сделки, пропущенные за окно обрыва: сигнатуры новее last_sig[w]."""
    loop = asyncio.get_event_loop()
    total = 0
    for w in wallets:
        until = last_sig.get(w)
        if not until:                     # ещё не видели этот кошелёк — нечего догружать
            continue
        try:
            res = await loop.run_in_executor(
                None, helius.rpc, "getSignaturesForAddress",
                [w, {"until": until, "limit": BACKFILL_LIMIT}])
            sigs = res.get("result") or []
        except Exception as e:  # noqa: BLE001
            print(f"[ws{label}] backfill fail {w[:8]}: {type(e).__name__}")
            continue
        for item in reversed(sigs):       # oldest→newest, сохраняем порядок
            if item.get("err"):
                continue
            sig = item.get("signature")
            if sig:
                total += 1
                await on_event(w, sig)
    if total:
        print(f"[ws{label}] backfill: догружено {total} пропущенных сделок")


async def subscribe_wallets(wallets: list[str], on_event: Callable[[str, str], Awaitable[None]],
                            label: str = "", on_fail=None) -> None:
    """on_fail(label, attempt, err) — зовётся при обрыве, чтобы монитор мог алертить.
    Инцидент 06.08: бот молчал час (Helius 429), а система не предупредила — узнали от владельца."""
    url = helius.ws_url()
    last_sig: dict[str, str] = {}     # wallet -> последняя обработанная сигнатура (переживает реконнект)
    attempt = 0
    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20, max_size=None) as ws:
                pending = {}   # request id -> wallet
                sub_to_wallet = {}   # subscription id -> wallet
                for i, w in enumerate(wallets):
                    pending[i] = w
                    await ws.send(json.dumps({
                        "jsonrpc": "2.0", "id": i, "method": "logsSubscribe",
                        "params": [{"mentions": [w]}, {"commitment": "confirmed"}],
                    }))
                print(f"[ws{label}] подписано {len(wallets)} кошельков")
                if attempt and on_fail:
                    try:
                        await on_fail(label, 0, "восстановлено")
                    except Exception:  # noqa: BLE001
                        pass
                attempt = 0                            # успешный коннект → сброс backoff
                await _backfill(wallets, last_sig, on_event, label)   # закрыть окно разрыва
                async for raw in ws:
                    m = json.loads(raw)
                    if "id" in m and "result" in m and not isinstance(m["result"], dict):
                        w = pending.get(m["id"])
                        if w is not None:
                            sub_to_wallet[m["result"]] = w
                        continue
                    if m.get("method") == "logsNotification":
                        val = m["params"]["result"]["value"]
                        if val.get("err"):
                            continue
                        w = sub_to_wallet.get(m["params"]["subscription"])
                        sig = val.get("signature")
                        if w and sig:
                            last_sig[w] = sig          # запомнить последнюю для backfill
                            await on_event(w, sig)
        except Exception as e:  # noqa: BLE001
            attempt += 1
            rate_limited = "429" in str(e)
            if rate_limited:
                # сервер прямо просит перестать: ждём долго и НЕ наращиваем попытки дальше,
                # иначе после снятия блокировки бэкофф останется огромным
                delay = RATE_LIMIT_PENALTY_S + random.uniform(0, BACKOFF_JITTER_S)
                attempt = min(attempt, 3)
            else:
                delay = (min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** (attempt - 1))
                         + random.uniform(0, BACKOFF_JITTER_S))
            print(f"[ws{label}] reconnect #{attempt} через {delay:.0f}с "
                  f"после {type(e).__name__}{' [RATE LIMIT]' if rate_limited else ''}: {str(e)[:80]}")
            if on_fail:
                try:
                    await on_fail(label, attempt, str(e)[:120])
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.sleep(delay)
