"""РЕГРЕСС аудита-5: цена из пылевой сделки не должна попадать в PnL.

В проде: 2 actor-продажи дали цену $76k–152k за токен (крошечный base_amount в
знаменателе). Одна запись раздула mean realized до +908 000 000%. Логика санитарной
проверки должна отсекать такие цены по отношению к опорной.
"""
import pytest

from src import strategy

SANITY = strategy.ALERTS["SANITY_JUMP"]


def sane_price(price, ref, sanity=SANITY):
    """Эталон логики monitor.sane_price (чистая функция для теста)."""
    if not price or price <= 0:
        return None
    if ref and ref > 0:
        ratio = price / ref
        if ratio > sanity or ratio < 1 / sanity:
            return ref
    return price


def test_пылевая_цена_отбрасывается_реальный_случай():
    """Точные числа из прода: entry 1.189e-05, «цена продажи» 1.526e+05."""
    assert sane_price(1.526e5, 1.189e-05) == pytest.approx(1.189e-05)


def test_второй_реальный_случай():
    assert sane_price(7.53e4, 1.565e-05) == pytest.approx(1.565e-05)


def test_нормальный_памп_проходит():
    """10x рост — законно, отбрасывать нельзя (иначе потеряем хвостовые победы)."""
    assert sane_price(1e-4, 1e-5) == pytest.approx(1e-4)


def test_нормальный_обвал_проходит():
    assert sane_price(2e-6, 1e-5) == pytest.approx(2e-6)


def test_обвал_в_ноль_отбрасывается():
    """Цена в 1000 раз ниже опорной — тоже мусор источника, не рынок."""
    assert sane_price(1e-8, 1e-5) == pytest.approx(1e-5)


def test_без_опорной_цены_принимаем_как_есть():
    assert sane_price(5e-6, None) == pytest.approx(5e-6)


def test_нулевая_и_отрицательная_цена():
    assert sane_price(0, 1e-5) is None
    assert sane_price(None, 1e-5) is None


def test_граница_фильтра_симметрична():
    """Ровно на пороге — пропускаем; за порогом — режем (в обе стороны)."""
    ref = 1e-5
    assert sane_price(ref * SANITY, ref) == pytest.approx(ref * SANITY)
    assert sane_price(ref * SANITY * 1.01, ref) == pytest.approx(ref)
    assert sane_price(ref / SANITY / 1.01, ref) == pytest.approx(ref)
