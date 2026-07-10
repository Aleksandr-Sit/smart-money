"""Async WS-клиент Helius: logsSubscribe(mentions) по батчу кошельков, callback на каждую tx.

Solana ограничение: mentions = ровно один адрес → одна подписка на кошелёк.
Free Helius: до 5 одновременных соединений; распределяем кошельки по ним.
Автопереподключение.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import websockets

from . import helius


async def subscribe_wallets(wallets: list[str], on_event: Callable[[str, str], Awaitable[None]],
                            label: str = "") -> None:
    url = helius.ws_url()
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
                            await on_event(w, sig)
        except Exception as e:  # noqa: BLE001
            print(f"[ws{label}] reconnect после {type(e).__name__}: {e}")
            await asyncio.sleep(3)
