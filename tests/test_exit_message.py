"""Сообщение о выходе: модель и деньги не должны стоять рядом без пометки.

Владелец 13.08 прислал алерт: MC вырос с $6,981 до $8,389, а realized −40%.
Противоречия не было — MC считались по ценам АКТОРА, а realized по нашим деньгам.
Цена выхода актора вдобавок недостижима: его продажа и обрушила рынок, а мы
продавали следом в ту же книгу и получили на 44% меньше.
"""
import pytest

from src import delivery, positions


def _поз():
    return positions.Position(token_mint="TOK", entry_price=6.98e-06, entry_ts=1000.0,
                              entry_actors=["A", "B"], entry_mc=6981.0, peak_price=6.98e-06)


def test_итог_по_деньгам_помечен_явно():
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", -0.3966,
                             модель=0.2018, разрыв=-0.5983)
    assert "ПО ДЕНЬГАМ -40%" in т
    assert "модель дала бы +20%" in т
    assert "РАЗРЫВ ИСПОЛНЕНИЯ -60%" in т


def test_цены_актора_названы_недоступными():
    """Главная причина путаницы: MC выхода это цена, которую получил АКТОР."""
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", -0.3966,
                             модель=0.2018, разрыв=-0.5983)
    assert "цены АКТОРА (нам недоступны)" in т
    assert "выход MC" in т and "8,39" in т   # округление до доллара не важно


def test_без_фактических_денег_сообщение_говорит_по_модели():
    """В бумажном режиме разрыва нет — и обещать «по деньгам» нельзя."""
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", 0.2018)
    assert "по модели +20%" in т
    assert "ПО ДЕНЬГАМ" not in т
    assert "РАЗРЫВ" not in т


def test_пик_и_выход_из_разных_источников_разведены():
    """В прежнем сообщении «пик 1.00x» стояло рядом с «выход 1.20x» — пик физически
    не может быть ниже выхода, если источник один. Источники разные, и это сказано."""
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", -0.4, модель=0.2, разрыв=-0.6)
    assert "пик по трекеру" in т


def test_запись_помечает_чьи_цены(monkeypatch):
    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    monkeypatch.setattr(delivery, "send_telegram", lambda *a, **k: None)
    delivery.deliver_exit(_поз(), 8.39e-06, "actors_exit", telegram=False,
                          realized_actual=-0.3966, exec_gap=-0.5983)
    rec = out[-1]
    assert rec["цены_источник"] == "актор"
    assert rec["realized_pnl"] == pytest.approx(-0.3966)
    assert rec["pnl_source"] == "деньги"
