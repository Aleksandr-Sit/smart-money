"""Фаза D — кошельковый слой. Единственное место, где живёт приватный ключ.

ПРИНЦИПЫ (на Solana нет биржи-арбитра: ключ = полный контроль над средствами,
поэтому защита строится на минимизации остатка и жёсткой структуре кода):
  1. Ключ читается ТОЛЬКО здесь, из .env, и НИКОГДА не логируется/не печатается/не
     уходит в Telegram. Ни целиком, ни частями.
  2. Модуль умеет: адрес, баланс, подпись транзакции. Больше ничего.
  3. Отсутствие ключа НЕ роняет монитор — бумажный режим работает без него (fail closed
     на торговлю, не на запуск).

Формат ключа в .env (SOLANA_PRIVATE_KEY): base58-строка (экспорт Phantom) ИЛИ
JSON-массив 64 байт (solana-keygen). Определяется автоматически.
"""
from __future__ import annotations

import json

from . import config, helius

_LAMPORTS = 1_000_000_000


def _load_keypair():
    """Ключ из .env → Keypair. None, если ключа нет (бумажный режим)."""
    raw = config.secret("SOLANA_PRIVATE_KEY", required=False)
    if not raw:
        return None
    from solders.keypair import Keypair
    raw = raw.strip()
    try:
        if raw.startswith("["):                       # JSON-массив от solana-keygen
            return Keypair.from_bytes(bytes(json.loads(raw)))
        return Keypair.from_base58_string(raw)        # экспорт Phantom
    except Exception as e:  # noqa: BLE001
        # НЕ печатаем ключ и не показываем его фрагменты — только тип ошибки
        raise RuntimeError(f"SOLANA_PRIVATE_KEY не распознан ({type(e).__name__}). "
                           f"Ожидается base58 или JSON-массив 64 байт") from None


class Wallet:
    """Горячий кошелёк. Держит МИНИМУМ средств — это главная защита (см. sweep.py)."""

    def __init__(self):
        self._kp = _load_keypair()

    @property
    def available(self) -> bool:
        return self._kp is not None

    @property
    def pubkey(self):
        if not self._kp:
            raise RuntimeError("ключ не задан (SOLANA_PRIVATE_KEY)")
        return self._kp.pubkey()

    @property
    def address(self) -> str:
        return str(self.pubkey)

    def balance_sol(self) -> float | None:
        """Баланс в SOL через RPC. None при сбое связи (вызывающий обязан считать это отказом)."""
        if not self._kp:
            return None
        try:
            r = helius.rpc("getBalance", [self.address])
            v = (r.get("result") or {}).get("value")
            return (v / _LAMPORTS) if v is not None else None
        except Exception:  # noqa: BLE001
            return None

    def sign(self, msg):
        """Подпись сообщения транзакции. Ключ наружу не отдаётся никогда."""
        if not self._kp:
            raise RuntimeError("ключ не задан")
        from solders.transaction import VersionedTransaction
        return VersionedTransaction(msg, [self._kp])

    def keypair(self):
        """Только для сборки транзакций внутри проекта. НЕ логировать результат."""
        if not self._kp:
            raise RuntimeError("ключ не задан")
        return self._kp


def status() -> str:
    w = Wallet()
    if not w.available:
        return "кошелёк не подключён (SOLANA_PRIVATE_KEY не задан) — бумажный режим"
    bal = w.balance_sol()
    return (f"кошелёк {w.address} · баланс "
            + (f"{bal:.4f} SOL" if bal is not None else "НЕДОСТУПЕН (RPC)"))


if __name__ == "__main__":
    print(status())
