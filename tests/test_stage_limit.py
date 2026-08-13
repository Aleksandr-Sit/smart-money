"""Предел ступени: замер на назначенном числе сделок обязан остановиться сам.

Возврат к живым деньгам идёт ступенями, и ступень 1 — это ЗАМЕР, а не торговля.
Без счётчика «замер на 100 сделках» превращается в бессрочную торговлю, потому что
никто не считает. Счётчик накопительный: смена UTC-суток его не обнуляет, иначе
предел не наступит никогда.
"""
import pytest

from src import risk


@pytest.fixture
def rm(tmp_path):
    def сделать(предел=0, банк=400.0, стоп=0.25):
        return risk.RiskManager(
            cfg={"BANKROLL_USD": банк, "CLIP_USD": 10.0, "DAILY_STOP_FRAC": стоп,
                 "MAX_POSITIONS": 2, "MAX_LOSS_STREAK": 99, "RISK_MODE": "enforce",
                 "STAGE_MAX_TRADES": предел},
            path=tmp_path / "risk.json")
    return сделать


def test_без_предела_поведение_прежнее(rm):
    r = rm(предел=0)
    for _ in range(50):
        assert r.on_close(0.01) is None
    assert r.state.halted is False


def test_предел_останавливает_ровно_на_назначенном(rm):
    r = rm(предел=5)
    for i in range(4):
        assert r.on_close(0.01) is None, f"остановка на {i+1}-й сделке преждевременна"
    сработал = r.on_close(0.01)
    assert сработал and "ступень завершена" in сработал["reason"]
    assert r.state.halted is True
    assert r.can_open(0)[0] is False


def test_счётчик_переживает_смену_суток(rm, monkeypatch):
    """Дневной сброс обнуляет realized_usd, но НЕ счёт ступени — иначе предел
    не наступит никогда при работе через полночь."""
    r = rm(предел=5)
    for _ in range(3):
        r.on_close(0.01)
    assert r.state.сделок_ступени == 3
    monkeypatch.setattr(risk, "_today", lambda: "2099-01-01")
    r._roll_day()
    assert r.state.сделок_ступени == 3, "счёт ступени обязан сохраниться"
    assert r.state.realized_usd == 0.0, "дневной счёт обязан обнулиться"
    r.on_close(0.01)
    r.on_close(0.01)
    assert r.state.halted is True


def test_остановка_ступени_не_снимается_новыми_сутками(rm, monkeypatch):
    """Это не дневной стоп: замер собран, и продолжать без разбора нельзя."""
    r = rm(предел=2)
    r.on_close(0.01)
    r.on_close(0.01)
    assert r.state.halted is True
    monkeypatch.setattr(risk, "_today", lambda: "2099-01-02")
    r._roll_day()
    assert r.state.halted is True, "смена суток не имеет права возобновить замер"


def test_предел_проверяется_раньше_дневного_стопа(rm):
    """Если сработали оба, причина должна быть «ступень завершена»: она точнее
    описывает, что делать дальше — разбирать, а не ждать полуночи."""
    r = rm(предел=2, банк=180.0, стоп=0.05)      # дневной лимит −$9
    assert r.on_close(-0.5) is None               # −$5, до лимита ещё есть запас
    сработал = r.on_close(-0.5)                   # −$10: сработали бы ОБА условия
    assert "ступень завершена" in сработал["reason"]


def test_состояние_переживает_перезапуск(rm, tmp_path):
    r = rm(предел=10)
    for _ in range(4):
        r.on_close(0.01)
    r2 = risk.RiskManager(
        cfg={"BANKROLL_USD": 400.0, "CLIP_USD": 10.0, "DAILY_STOP_FRAC": 0.25,
             "MAX_POSITIONS": 2, "MAX_LOSS_STREAK": 99, "RISK_MODE": "enforce",
             "STAGE_MAX_TRADES": 10},
        path=tmp_path / "risk.json")
    assert r2.state.сделок_ступени == 4, "перезапуск не должен обнулять замер"
