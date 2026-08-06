"""Инвариант: сделка берётся из логов уведомления, а не из платного getTransaction.

Регрессия для инцидента 06.08 — getTransaction на каждое событие (242k/сутки) сжёг
месячный лимит Helius за 4 дня, хотя те же данные уже лежали в push-уведомлении.
"""
import base64
import struct

from src import helius, log_parse

MINT = bytes(range(32))
BUY_LOG = "Program log: Instruction: Buy"
PROG = f"Program {log_parse.PUMP} invoke [1]"


def _event(sol_lamports: int, tokens: int, is_buy: bool = True) -> str:
    raw = (b"\x00" * 8 + MINT + struct.pack("<QQ", sol_lamports, tokens)
           + bytes([1 if is_buy else 0]) + b"\x00" * 32)
    return "Program data: " + base64.b64encode(raw).decode()


def test_разбирает_покупку_из_логов():
    t = log_parse.parse_logs([PROG, BUY_LOG, _event(2_000_000_000, 5_000_000)], "sig1")
    assert t is not None
    assert t["side"] == "buy"
    assert t["sol"] == 2.0
    assert t["base_amount"] == 5.0
    assert t["source"] == "logs"          # источник помечен: видно, платили ли за сделку
    assert t["signature"] == "sig1"


def test_разбирает_продажу():
    logs = [PROG, "Program log: Instruction: Sell", _event(500_000_000, 1_000_000, False)]
    assert log_parse.parse_logs(logs)["side"] == "sell"


def test_чужая_программа_не_сделка():
    """Гейт обязан отсекать до разбора, иначе вернётся мусорный токен."""
    assert not log_parse.is_trade(["Program Vote111 invoke [1]", BUY_LOG])
    assert log_parse.parse_logs(["Program Vote111 invoke [1]", BUY_LOG]) is None


def test_мусор_не_роняет_разбор():
    for logs in ([], ["Program data: не-base64!!!"], [PROG, BUY_LOG, "Program data: AAAA"],
                 [PROG, BUY_LOG, _event(0, 0)]):
        assert log_parse.parse_logs(logs) is None


def test_base58_совпадает_с_известным_адресом():
    """Свой b58 обязан давать тот же адрес, что и сеть, иначе купим не тот токен."""
    from solders.pubkey import Pubkey
    assert log_parse.b58encode(bytes(Pubkey.from_string(log_parse.PUMP))) == log_parse.PUMP


def test_бюджет_режет_дорогие_вызовы():
    """Предохранитель против повторения инцидента: шторм backfill не должен сжечь лимит."""
    helius._spent.clear()
    cap = helius._HOURLY_CAP["getTransaction"]
    assert all(helius.budget_ok("getTransaction") for _ in range(cap))
    assert not helius.budget_ok("getTransaction")     # дальше — отказ, а не расход
    assert helius.budget_ok("getBalance")             # недорогие методы не ограничены
    helius._spent.clear()
