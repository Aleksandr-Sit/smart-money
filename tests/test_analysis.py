"""Отсечка аномалий при чтении журналов (П5 аудита 08.08).

Четыре июльские записи с ценой до $153 000 за токен делают среднее по всем
сделкам равным +177 335 806% против +19.7% без них. Журнал сырых событий
переписывать нельзя — фильтровать надо при чтении.
"""
import json

import pytest

from src import analysis


@pytest.fixture
def out(tmp_path):
    rows = [
        {"type": "exit", "ts": "2026-08-01T10:00:00+00:00", "entry_ts": 1785000000,
         "token_mint": "A", "realized_pnl": 0.5},
        {"type": "exit", "ts": "2026-08-01T11:00:00+00:00", "entry_ts": 1785003600,
         "token_mint": "B", "realized_pnl": -0.4},
        {"type": "exit", "ts": "2026-07-30T15:42:44+00:00", "entry_ts": 1784900000,
         "token_mint": "C", "realized_pnl": 12832014201.0},      # боевая аномалия
        {"type": "exit", "ts": "2026-08-01T12:00:00+00:00", "entry_ts": 1785007200,
         "token_mint": "D", "realized_pnl": 5.93},               # +593% — реальный максимум
    ]
    p = tmp_path / "paper_closed.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\nбитая строка\n", encoding="utf-8")
    return tmp_path


def test_аномалия_отсекается_реальная_прибыль_остаётся(out):
    rows = analysis.load_closed(out, with_fee=False)
    mints = [r["token_mint"] for r in rows]
    assert "C" not in mints          # +1 283 201 420% отсечена
    assert "D" in mints              # +593% — настоящий рекорд, остаётся
    assert len(rows) == 3


def test_битая_строка_не_роняет_чтение(out):
    """Запись обрывается при рестарте контейнера — это норма, падать нельзя."""
    assert len(analysis.read_jsonl("paper_closed.jsonl", out)) == 4


def test_комиссия_вычитается_по_умолчанию(out):
    from src import strategy
    fee = strategy.RISK["EXIT_FEE"]
    a = analysis.load_closed(out, with_fee=True)
    b = analysis.load_closed(out, with_fee=False)
    assert a[0]["pnl"] == pytest.approx(b[0]["pnl"] - fee)


def test_границы_периода_исключают_окно_дефекта(out):
    """Нужно уметь выкидывать интервал с известным багом кода."""
    rows = analysis.load_closed(out, since=1785003000, with_fee=False)
    assert [r["token_mint"] for r in rows] == ["B", "D"]


def test_порог_не_пограничный(out):
    """Между максимальной корректной сделкой и минимальной аномалией должен быть
    разрыв — иначе фильтр режет настоящую прибыль."""
    rep = analysis.anomaly_report(out)
    assert rep["отсечено"] == 1
    assert rep["максимум_корректной"] < analysis.MAX_ABS_PNL
    assert rep["минимум_отсечённой"] > analysis.MAX_ABS_PNL * 10


def test_сортировка_по_времени_выхода(out):
    rows = analysis.load_closed(out, with_fee=False)
    assert [r["exit_ts"] for r in rows] == sorted(r["exit_ts"] for r in rows)
