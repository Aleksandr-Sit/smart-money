"""Тесты логики выхода: частичные тейки, приоритет правил, actor-exit, учёт PnL.

Критичный путь для реальных денег: ошибка здесь = неверный размер позиции или
неверно посчитанная прибыль.
"""
import pytest

from src.positions import PositionManager, total_realized


@pytest.fixture
def pm(tmp_path, monkeypatch):
    """Изолированный менеджер: стейт в tmp, не трогаем боевой output/.

    Тейки, стоп и порог акторов задаём ЯВНО: в боевом конфиге тейки и стоп
    отключены (07.08), порог снижен до 0.25 (08.08). Механизмы остаются в коде и
    обязаны работать при любых значениях, поэтому тест механизма не должен
    зависеть от текущей настройки. Боевые значения проверяются отдельными тестами.
    """
    from src import strategy
    m = PositionManager({**strategy.EXIT, "PARTIAL_TAKES": [(2.0, 0.5)],
                         "SL_MULT": 0.5, "EXIT_ACTOR_FRAC": 0.5})
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


def test_стоп_отключается_нулём(tmp_path):
    """SL_MULT=0 — стоп не срабатывает НИКОГДА, включая цену 0.

    07.08: стоп убран по замеру (+672$). Проверка на 0 нужна явно — условие
    `mult <= 0` при нулевой цене закрыло бы позицию «стопом», которого нет.
    Мёртвый токен закрывается отдельным правилом DEAD_AGE_H.
    """
    from src import strategy
    pm = PositionManager({**strategy.EXIT, "SL_MULT": 0.0, "PARTIAL_TAKES": []})
    pm.path = tmp_path / "p.json"
    pm.pos = {}
    pm.open("TOK", 0.001, 1e6, ["a1", "a2"], 1000)
    assert pm.check_price("TOK", 0.0001, 0.1) is None      # −90% и позиция жива
    assert pm.check_price("TOK", 0.0, 0.1) is None         # цена 0 — не «стоп»


def test_стоп_работает_если_его_вернуть(tmp_path):
    """Механизм остаётся рабочим: положительный SL_MULT снова закрывает позицию."""
    from src import strategy
    pm = PositionManager({**strategy.EXIT, "SL_MULT": 0.5, "PARTIAL_TAKES": []})
    pm.path = tmp_path / "p.json"
    pm.pos = {}
    pm.open("TOK", 0.001, 1e6, ["a1", "a2"], 1000)
    assert pm.check_price("TOK", 0.0004, 0.1)["reason"] == "stop_loss"


def test_боевой_конфиг_без_стопа_и_тейка():
    """Фиксируем принятое решение, чтобы случайный откат конфига был заметен."""
    from src import strategy
    assert strategy.EXIT["SL_MULT"] == 0.0
    assert strategy.EXIT["PARTIAL_TAKES"] == []
    assert strategy.EXIT["TP_MULT"] == 6.0        # тейк 6x остаётся
    assert strategy.EXIT["MAX_HOLD_S"] == 1800    # таймаут остаётся


def test_порог_выхода_одна_продажа_до_четырёх_акторов(tmp_path):
    """08.08: FRAC 0.25 — одной продажи достаточно при 2–4 акторах.

    Раньше при 0.5 позиция «вышел 1 из 3» продолжала висеть до таймаута с
    медианой −40.2%. Мы следуем за акторами на входе — следуем и на выходе.
    """
    from src import strategy
    for n_actors, need in ((2, 1), (3, 1), (4, 1), (5, 2), (6, 2)):
        pm = PositionManager({**strategy.EXIT, "EXIT_ACTOR_FRAC": 0.25})
        pm.path = tmp_path / f"p{n_actors}.json"
        pm.pos = {}
        actors = [f"a{i}" for i in range(n_actors)]
        pm.open("TOK", 0.001, 1e6, actors, 1000)
        for i in range(need - 1):
            assert pm.on_sell("TOK", actors[i]) is None, f"{n_actors} акт.: выход на {i+1}-й рано"
        assert pm.on_sell("TOK", actors[need - 1]) == "actors_exit", \
            f"{n_actors} акторов: выход должен быть на {need}-й продаже"


def test_повторная_продажа_того_же_актора_не_считается(tmp_path):
    """Иначе один актор, продающий частями, выбил бы позицию досрочно."""
    from src import strategy
    pm = PositionManager({**strategy.EXIT, "EXIT_ACTOR_FRAC": 0.25})
    pm.path = tmp_path / "p.json"
    pm.pos = {}
    pm.open("TOK", 0.001, 1e6, ["a1", "a2", "a3", "a4", "a5"], 1000)   # need = 2
    assert pm.on_sell("TOK", "a1") is None
    assert pm.on_sell("TOK", "a1") is None          # тот же актор второй раз
    assert pm.on_sell("TOK", "a2") == "actors_exit"


def test_боевой_порог_зафиксирован():
    from src import strategy
    assert strategy.EXIT["EXIT_ACTOR_FRAC"] == 0.25
