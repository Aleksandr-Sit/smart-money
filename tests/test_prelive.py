"""РЕГРЕССЫ аудита-6 — три дефекта, критичных именно для реальных денег."""
import pytest

from src import price_track, strategy
from src.positions import PositionManager, total_realized
from src.signal_engine import BuyEvent, SignalEngine

MAX_EVENT_AGE_S = 300


# ---------- 1) потеря трекинга после рестарта ----------
def test_перерегистрация_возвращает_позицию_под_трекинг():
    """После рестарта позиция «слепла»: трекер её не вёл → таймаут по цене 0 = −100%
    (в проде 18 таких выходов, −$180). renew=True возвращает трекинг с текущего момента."""
    t = price_track.PriceTracker()
    mint = "FeSENori1vjgUYP63oPPeNJtjXeLpm9sht8UN2k4pump"
    t.register(mint, 1e-5, ts=1000.0)          # «старая» регистрация
    assert t.active[mint]["t0"] == 1000.0
    t.register(mint, None, renew=True)          # после рестарта
    import time
    assert t.active[mint]["t0"] > time.time() - 5    # окно трекинга отсчитывается заново


def test_повторная_регистрация_без_renew_не_сбрасывает():
    t = price_track.PriceTracker()
    mint = "FeSENori1vjgUYP63oPPeNJtjXeLpm9sht8UN2k4pump"
    t.register(mint, 1e-5, ts=1000.0)
    t.register(mint, 2e-5, ts=2000.0)           # обычный повтор игнорируется
    assert t.active[mint]["t0"] == 1000.0


# ---------- 2) выход без цены не должен списываться в −100% ----------
def test_выход_по_последней_известной_а_не_в_ноль(tmp_path):
    pm = PositionManager()
    pm.path = tmp_path / "p.json"
    pm.pos = {}
    pm.open("TOK", 1e-5, 1e6, ["a1"], 1000)
    p = pm.get("TOK")
    assert total_realized(p, None) == pytest.approx(-1.0)       # старое поведение = −100%
    last_known = 8e-6                                            # трекер знал цену −20%
    assert total_realized(p, last_known) == pytest.approx(-0.2)  # честнее нуля


# ---------- 3) протухшие события (WS-backfill) ----------
@pytest.mark.parametrize("age_s,should_trade", [(2, True), (60, True), (299, True),
                                                (301, False), (38250, False)])
def test_гейт_возраста_события(age_s, should_trade):
    """Замерено: 11 из 9276 сигналов имели задержку >10 мин, максимум 10.6 ЧАСА —
    это backfill проигрывал старые сигнатуры. Вход по такой цене = плохая сделка."""
    assert (age_s <= MAX_EVENT_AGE_S) is should_trade


# ---------- мёртвый MC-гейт удалён ----------
def test_мёртвый_mc_гейт_не_режет_сигналы():
    """Гейт никогда не срабатывал (BuyEvent без token_mc), а включать его ВРЕДНО:
    MC>100k — лучшая когорта (win 0.57 против 0.42). Проверяем, что он не вернулся."""
    amap = {"w1": ("a1", 10.0), "w2": ("a2", 8.0)}
    eng = SignalEngine(amap)
    eng.process(BuyEvent(100, "T", "w1", usd=100, token_mc=5_000_000))
    sig = eng.process(BuyEvent(110, "T", "w2", usd=100, token_mc=5_000_000))
    assert sig is not None and sig.n_actors == 2      # огромный MC сигнал НЕ блокирует


def test_потолок_mc_остаётся_в_мониторе():
    """Реальная защита — monitor --max-mc; в конфиге движка мёртвых гейтов быть не должно."""
    import inspect
    from src import signal_engine
    src = inspect.getsource(signal_engine.SignalEngine.process)
    assert "SIGNAL_MAX_MC_USD" not in src and "SIGNAL_MAX_AGE_S" not in src
