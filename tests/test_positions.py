"""Тесты логики выхода: частичные тейки, приоритет правил, actor-exit, учёт PnL.

Критичный путь для реальных денег: ошибка здесь = неверный размер позиции или
неверно посчитанная прибыль.
"""
import pytest

from src.positions import PositionManager, total_realized


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """Изолированный менеджер: стейт в tmp, не трогаем боевой output/."""
    m = PositionManager()
    m.path = tmp_path / "open_positions.json"
    m.pos = {}
    return m


def _open(pm, price=0.001, actors=("a1", "a2")):
    assert pm.open("TOK", price, 1e6, list(actors), 1000)
    return pm.get("TOK")


def test_частичный_тейк_продаёт_долю_и_позиция_живёт(pm):
    p = _open(pm)
    res = pm.check_price("TOK", 0.002, 0.1)          # 2x → тейк 50%
    assert res == {"action": "partial", "reason": "take_partial", "frac": 0.5}
    assert p.remaining == pytest.approx(0.5)
    assert p.realized == pytest.approx(0.5)          # 0.5 * (2-1)
    assert "TOK" in pm.open_tokens()                 # позиция НЕ закрыта


def test_частичный_тейк_срабатывает_один_раз(pm):
    _open(pm)
    pm.check_price("TOK", 0.002, 0.1)
    assert pm.check_price("TOK", 0.0021, 0.1) is None   # повторно на том же уровне — нет


def test_итоговый_pnl_учитывает_частичный_и_остаток(pm):
    p = _open(pm)
    pm.check_price("TOK", 0.002, 0.1)                # +0.5 реализовано, остаток 0.5
    assert pm.check_price("TOK", 0.006, 0.1)["reason"] == "take_profit"
    assert total_realized(p, 0.006) == pytest.approx(3.0)   # 0.5 + 0.5*(6-1)


def test_стоп_лосс_без_частичного(pm):
    p = _open(pm)
    assert pm.check_price("TOK", 0.0004, 0.1)["reason"] == "stop_loss"
    assert total_realized(p, 0.0004) == pytest.approx(-0.6)


def test_dead_без_цены_даёт_минус_100(pm):
    """Раньше dead писался как None и МОЛЧА исключался из метрик (аудит-4)."""
    p = _open(pm)
    assert total_realized(p, None) == pytest.approx(-1.0)


def test_трейлинг_только_после_взвода(pm):
    _open(pm)
    pm.check_price("TOK", 0.0014, 0.1)               # 1.4x — ниже TRAIL_ARM=1.5, не взведён
    assert pm.check_price("TOK", 0.0009, 0.1) is None
    pm.check_price("TOK", 0.0019, 0.1)               # взводим пик 1.9x (тейка нет, <2x)
    assert pm.check_price("TOK", 0.0012, 0.1)["reason"] == "trailing"   # -37% от пика


def test_таймаут_срабатывает_по_возрасту(pm):
    _open(pm)
    assert pm.check_price("TOK", 0.0011, 0.4) is None          # 24 мин < 30
    assert pm.check_price("TOK", 0.0011, 0.6)["reason"] == "timeout"


def test_actor_exit_порог_и_идемпотентность(pm):
    _open(pm, actors=("a1", "a2", "a3"))
    assert pm.on_sell("TOK", "a1") is None            # 1/3 < 50%
    assert pm.on_sell("TOK", "a1") is None            # повтор того же актора не считается
    assert pm.on_sell("TOK", "a2") == "actors_exit"   # 2/3 >= 50%


def test_actor_exit_чужой_актор_игнорируется(pm):
    _open(pm)
    assert pm.on_sell("TOK", "чужой") is None


def test_нельзя_открыть_дубль_или_нулевую_цену(pm):
    _open(pm)
    assert pm.open("TOK", 0.002, 1e6, ["a1"], 1000) is False    # дубль
    assert pm.open("NEW", 0, 1e6, ["a1"], 1000) is False        # нулевая цена


def test_стейт_переживает_перезагрузку(pm, tmp_path):
    p = _open(pm)
    pm.check_price("TOK", 0.002, 0.1)                 # частичный тейк
    reloaded = PositionManager()
    reloaded.path = pm.path
    reloaded.pos = {}
    reloaded._load()
    r = reloaded.get("TOK")
    assert r is not None and r.remaining == pytest.approx(0.5) and r.realized == pytest.approx(0.5)
