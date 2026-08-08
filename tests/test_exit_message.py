"""Сообщение о выходе обязано быть самодостаточным.

С 07.08 правило входа "all": торгуем каждый сигнал, но уведомления о ВХОДЕ уходят
только для отобранных классов. Выходы объявляются все. Владелец 08.08 не смог найти,
когда и по чему бот вошёл в токен, о выходе из которого пришло сообщение. Слать вход
по каждой сделке нельзя (~200 сообщений в сутки), поэтому пара восстанавливается
из самого сообщения о выходе.
"""
import time

from src.delivery import format_exit
from src.positions import Position


def _pos(**kw):
    d = dict(token_mint="ASirAQA5UvfeAyxKwKVU7tYfdU6x28t428EEKDKopump",
             entry_ts=time.time() - 1800, entry_price=8.584e-06, entry_mc=8584.0,
             entry_actors=["a1", "a2"], peak_price=1.219e-05)
    d.update(kw)
    return Position(**d)


def test_вход_виден_в_сообщении_о_выходе():
    m = format_exit(_pos(), 2.108e-06, "timeout", -0.754)
    assert "ВХОД" in m and "UTC" in m
    assert "акторов 2" in m          # состав входа
    assert "$8,584" in m             # MC входа
    assert "держали 30 мин" in m     # время удержания


def test_пик_и_выход_в_кратности_входа():
    """Без пика непонятно, была ли позиция в плюсе — а это главный вопрос
    при разборе убыточного выхода."""
    m = format_exit(_pos(), 2.108e-06, "timeout", -0.754)
    assert "пик 1.42x" in m
    assert "выход 0.25x" in m


def test_короткое_удержание_в_секундах():
    """Медиана удержания 24с — показывать «0 мин» бессмысленно."""
    m = format_exit(_pos(entry_ts=time.time() - 17), 1.0e-05, "actors_exit", 0.17)
    assert "держали 17 с" in m


def test_без_цены_выхода_не_падает():
    """Цены может не быть (токен ослеп) — сообщение всё равно должно уйти."""
    m = format_exit(_pos(), 0.0, "dead", -1.0)
    assert "выход ?" in m and "token:" in m


def test_без_времени_входа_не_падает():
    m = format_exit(_pos(entry_ts=0), 2.1e-06, "timeout", -0.75)
    assert "ВХОД ?" in m
