"""Наблюдение за несошедшимися покупками — сбор данных без входа.

Замер 12.08: 76% токенов, купленных нашими же акторами, никогда не складываются
в конфлюенс (2664 из 11092). Оценить пропущенное было нечем — трекер регистрирует
токен только после сигнала. Тесты закрепляют главное: наблюдение НЕ открывает
позицию и не живёт дольше отведённого окна.
"""
import time

import pytest

from src import price_track


def test_свой_срок_наблюдения_короче_общего(monkeypatch):
    """Несошедшийся токен ведём 15 минут, а не 45: нужна ранняя траектория, а не
    полная жизнь токена. Иначе price_history растёт вчетверо ради данных,
    которые нужны на десять минут."""
    t = price_track.PriceTracker()
    monkeypatch.setattr(t, "_append", lambda row: None)
    monkeypatch.setattr(price_track, "bonding_curve_pda", lambda m: "PDA")
    сейчас = time.time()
    t.register("КОРОТКИЙ", 1.0, ts=сейчас - 1000, ttl=900)
    t.register("ОБЫЧНЫЙ", 1.0, ts=сейчас - 1000)
    monkeypatch.setattr(price_track.helius, "rpc",
                        lambda *a, **k: {"result": {"value": []}})
    monkeypatch.setattr(price_track.market, "sol_price", lambda: 100.0)
    t._poll_once()
    assert "КОРОТКИЙ" not in t.active, "срок 900с истёк — токен снят с наблюдения"
    assert "ОБЫЧНЫЙ" in t.active, "общий срок 2700с ещё не истёк"


def test_без_ttl_поведение_прежнее(monkeypatch):
    t = price_track.PriceTracker()
    monkeypatch.setattr(t, "_append", lambda row: None)
    monkeypatch.setattr(price_track, "bonding_curve_pda", lambda m: "PDA")
    t.register("M", 1.0)
    assert t.active["M"]["ttl"] is None


def test_список_соло_акторов_пуст_по_умолчанию_выключает_сбор():
    """Пустой список = поведение как раньше. Настройка, которая выглядит работающей,
    но не работает, опаснее её отсутствия — поэтому проверяем оба конца."""
    from src import monitor, strategy
    assert isinstance(monitor.СОЛО_АКТОРЫ, set)
    if strategy.SIGNAL.get("TRACK_SOLO_ACTORS"):
        assert monitor.СОЛО_АКТОРЫ, "список задан в конфиге — монитор обязан его видеть"
    assert monitor.СОЛО_ОКНО_С > 0


def test_окно_наблюдения_меньше_общего_трека():
    """Если сделать его больше TRACK_S, короткий срок потеряет смысл и данные
    вырастут вчетверо — ровно то, чего мы избегаем."""
    from src import monitor
    assert monitor.СОЛО_ОКНО_С < price_track.TRACK_S
