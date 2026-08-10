"""Helius Free — обёртка RPC/WS. Ключ HELIUS_API_KEY из .env, НИКОГДА не логируется.

Стандартные эндпоинты (free-тариф): RPC POST + WebSocket logsSubscribe/accountSubscribe.
"""
from __future__ import annotations

import time
import requests

from .config import secret

RPC_HOST = "mainnet.helius-rpc.com"
# Публичный узел Solana — запасной провайдер без квоты (инцидент 06.08: лимит Helius
# исчерпан на 26 дней). Подписок принимает ~50 на соединение при ПЛАВНОЙ отправке.
PUBLIC_RPC = "https://api.mainnet-beta.solana.com"
PUBLIC_WS = "wss://api.mainnet-beta.solana.com"


# ПРЕДОХРАНИТЕЛЬ (инцидент 06.08): без верхней границы один шторм реконнектов
# сжёг месячный лимит за 4 дня. Считаем дорогие вызовы и жёстко режем их сверху —
# лучше потерять часть backfill, чем ослепнуть на 26 дней.
_HOURLY_CAP = {"getTransaction": 2000, "getSignaturesForAddress": 300}
_spent: dict[str, list] = {}


def budget_ok(method: str) -> bool:
    """False, если метод исчерпал часовой лимит. Счётчик скользит по часу."""
    cap = _HOURLY_CAP.get(method)
    if cap is None:
        return True
    now = time.time()
    hour, used = _spent.get(method, (0.0, 0))
    if now - hour >= 3600:
        hour, used = now, 0
    if used >= cap:
        return False
    _spent[method] = (hour, used + 1)
    return True


def budget_report() -> str:
    now = time.time()
    parts = []
    for m, (hour, used) in _spent.items():
        if now - hour < 3600:
            parts.append(f"{m.replace('get', '')}={used}/{_HOURLY_CAP[m]}")
    return " ".join(parts) or "0"


def _provider() -> str:
    from . import strategy
    return strategy.TRACKING.get("RPC_PROVIDER", "helius")


DRPC_HOST = "lb.drpc.live"
# Замер 07.08 с сервера (Франкфурт): метод открыт, отклик 0.17с. Другой пул, не тот,
# откуда мы читаем — падение одного не выключает и чтение, и отправку разом.
PUBLICNODE_RPC = "https://solana-rpc.publicnode.com"
# Jito — блок-энджин, отдельный путь к валидаторам, не тот пул, откуда мы читаем.
# Замер 07.08 с сервера во Франкфурте: отклик 0.04с против 0.08с у публичного узла.
# Лимит по частоте жёсткий: пять запросов подряд дали 429, с паузой 1.2с метод открыт.
JITO_SEND = "https://frankfurt.mainnet.block-engine.jito.wtf/api/v1/transactions"


def send_url() -> str:
    """Узел для ОТПРАВКИ сделок — намеренно ОТДЕЛЬНЫЙ от узла чтения.

    Смысл разделения не в том, что бесплатный узел «плохой»: confirm() работает
    fail-closed, поэтому неудачная отправка стоит пропущенной сделки, а не денег.
    Смысл в том, что при падении общего узла бот одновременно ослепнет и потеряет
    возможность закрыть открытые позиции. Разные пулы разносят этот риск.
    """
    from . import strategy
    who = strategy.EXECUTION.get("SEND_PROVIDER", "helius")
    if who == "public":
        return PUBLIC_RPC
    if who == "publicnode":
        return PUBLICNODE_RPC
    if who == "jito":
        return JITO_SEND
    if who == "drpc":
        return f"https://{DRPC_HOST}/solana/{secret('DRPC_API_KEY')}"
    return f"https://{RPC_HOST}/?api-key={secret('HELIUS_API_KEY')}"


def rpc_url() -> str:
    if _provider() == "public":
        return PUBLIC_RPC
    return f"https://{RPC_HOST}/?api-key={secret('HELIUS_API_KEY')}"


def ws_url() -> str:
    if _provider() == "public":
        return PUBLIC_WS
    return f"wss://{RPC_HOST}/?api-key={secret('HELIUS_API_KEY')}"


# Методы, отправляющие деньги: идут на send_url(), а не на общий узел чтения
_SEND_METHODS = {"sendTransaction"}


def rpc(method: str, params: list, timeout: int = 20, url: str | None = None) -> dict:
    """JSON-RPC вызов. ОШИБКА УЗЛА = ИСКЛЮЧЕНИЕ, а не пустой результат.

    Найдено пробной живой сделкой 10.08. Раньше при ответе вида {"error": {...}}
    (например, 429 от публичного узла) метод возвращал словарь без ключа "result",
    а вызывающий делал .get("result") и получал None. Для getTokenAccountsByOwner
    это неотличимо от «аккаунта нет» — то есть сбой сети выглядел как «токенов ноль».
    Именно так покупка на $10 прошла на цепи, а код решил, что баланс не вырос.
    Все вызывающие уже обёрнуты в try/except, поэтому громкий отказ безопаснее тихой лжи.

    url — прочитать с КОНКРЕТНОГО узла (нужно, когда узел чтения отстаёт от узла,
    на который мы только что отправили транзакцию).
    """
    dest = url or (send_url() if method in _SEND_METHODS else rpc_url())
    r = requests.post(dest, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                      timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("error") is not None:
        err = data["error"]
        raise RuntimeError(f"RPC {method}: {str(err)[:200]}")
    return data


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
