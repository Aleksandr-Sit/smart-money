"""Helius Free — обёртка RPC/WS. Ключ HELIUS_API_KEY из .env, НИКОГДА не логируется.

Стандартные эндпоинты (free-тариф): RPC POST + WebSocket logsSubscribe/accountSubscribe.
"""
from __future__ import annotations

import requests

from .config import secret

RPC_HOST = "mainnet.helius-rpc.com"


def rpc_url() -> str:
    return f"https://{RPC_HOST}/?api-key={secret('HELIUS_API_KEY')}"


def ws_url() -> str:
    return f"wss://{RPC_HOST}/?api-key={secret('HELIUS_API_KEY')}"


def rpc(method: str, params: list, timeout: int = 20) -> dict:
    r = requests.post(rpc_url(), json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=timeout)
    r.raise_for_status()
    return r.json()


def check() -> None:
    """Проверка связи/ключа (getSlot, getHealth). Ключ не печатается."""
    try:
        slot = rpc("getSlot", []).get("result")
        health = rpc("getHealth", []).get("result")
        print(f"[helius] OK — slot={slot}, health={health}")
    except Exception as e:  # noqa: BLE001
        print(f"[helius] FAIL: {type(e).__name__}: {e}")


if __name__ == "__main__":
    check()
