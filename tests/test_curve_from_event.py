"""Цена кривой прямо из события pump.fun — без единого запроса к узлу.

Найдено 12.08 при разборе расхождения «цена актора против цены кривой». За полем
user в TradeEvent идут timestamp и виртуальные резервы ПОСЛЕ сделки. Проверка на
живой транзакции: цена кривой после покупки = 1.0019 от цены исполнения — ровно
как предписывает математика бондинг-кривой, то есть разбор цены был верен, а
5.5% разрыва до первого тика трекера это НАСТОЯЩЕЕ движение цены, а не артефакт.
"""
import base64
import struct

import pytest

from src import log_parse


def _событие(mint32: bytes, user32: bytes, sol: int, tokens: int, is_buy: bool,
             vsol: int | None = None, vtok: int | None = None) -> str:
    raw = bytes(8) + mint32 + struct.pack("<QQ", sol, tokens) + bytes([int(is_buy)]) + user32
    if vsol is not None:
        raw += struct.pack("<qQQ", 1_700_000_000, vsol, vtok)
    return "Program data: " + base64.b64encode(raw).decode()


МЯТА = bytes(range(32))
КОШ = bytes(range(32, 64))


def test_цена_кривой_читается_из_события():
    """30 SOL и 1 000 000 000 токенов → 3e-8 SOL за токен."""
    лог = _событие(МЯТА, КОШ, sol=10**9, tokens=10**6, is_buy=True,
                   vsol=30 * 10**9, vtok=10**9 * 10**6)
    e = log_parse._events([лог])[0]
    assert e["curve_price_sol"] == pytest.approx(3e-8)


def test_старое_короткое_событие_не_ломает_разбор():
    """До появления резервов события были короче. Отсутствие поля — не ошибка,
    а None: подставить сюда цену сделки значило бы тихо смешать две величины."""
    лог = _событие(МЯТА, КОШ, sol=10**9, tokens=10**6, is_buy=True)
    e = log_parse._events([лог])[0]
    assert e["curve_price_sol"] is None
    assert e["lamports"] == 10**9


def test_нулевые_резервы_дают_none():
    лог = _событие(МЯТА, КОШ, sol=10**9, tokens=10**6, is_buy=True, vsol=0, vtok=0)
    assert log_parse._events([лог])[0]["curve_price_sol"] is None


def test_цена_кривой_доезжает_до_разбора_сделки():
    """parse_logs — то, чем пользуется монитор. Без проброса поля находка осталась
    бы в приватной функции и никому бы не досталась."""
    from src.log_parse import b58encode
    лог = _событие(МЯТА, КОШ, sol=10**9, tokens=10**6, is_buy=True,
                   vsol=30 * 10**9, vtok=10**9 * 10**6)
    логи = [f"Program {log_parse.PUMP} invoke [1]",
            "Program log: Instruction: Buy", лог]
    t = log_parse.parse_logs(логи, "sig", b58encode(КОШ))
    assert t is not None
    assert t["curve_price_sol"] == pytest.approx(3e-8)


def test_цена_сделки_и_цена_кривой_это_РАЗНЫЕ_величины():
    """Цена исполнения ниже цены кривой после покупки — покупатель идёт вверх по
    кривой. Совпадение этих чисел означало бы ошибку разбора."""
    лог = _событие(МЯТА, КОШ, sol=10**9, tokens=10**6, is_buy=True,
                   vsol=30 * 10**9, vtok=10**9 * 10**6)
    e = log_parse._events([лог])[0]
    цена_сделки = (e["lamports"] / 1e9) / (e["tokens"] / 1e6)
    assert цена_сделки != pytest.approx(e["curve_price_sol"])


def test_цена_кривой_попадает_в_журнал_покупок(monkeypatch):
    """Без записи в журнал находка не даёт ничего: разрыв «цена актора против рынка»
    надо измерять на каждой покупке, а не восстанавливать задним числом."""
    from src import delivery
    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    delivery.log_actor_buy("A", "W", "TOK", 10.0, 1.0, False, 0,
                           price=4.0e-06, curve_price=3.8e-06)
    rec = out[0]
    assert rec["curve_price"] == pytest.approx(3.8e-06)
    assert rec["price"] == pytest.approx(4.0e-06)


def test_без_резервов_поле_есть_и_равно_None(monkeypatch):
    """Поле обязано присутствовать всегда — иначе анализ будет молча пропускать
    старые строки, не зная почему."""
    from src import delivery
    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    delivery.log_actor_buy("A", "W", "TOK", 10.0, 1.0, False, 0)
    assert "curve_price" in out[0] and out[0]["curve_price"] is None
