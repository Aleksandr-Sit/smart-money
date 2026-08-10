"""Итог сделки считается по ДЕНЬГАМ, а не по цене трекера (10.08).

Модель брала цену трекера в момент срабатывания правила. При actor-exit это цена
АКТОРА, за которым мы идём: он продал, продавил книгу, и только потом продаём мы.
Замер на 52 живых сделках за пять часов:

    модель (цена трекера)   +$99.10   медиана  −0.9%
    факт (деньги)           +$37.63   медиана −11.9%
    разрыв                  −$61.48   медиана  −9.8%

Собственный порог смерти edge из аудита-3 — хайркат 12%. Пока PnL считался по модели,
каждый замер стратегии был завышен примерно на эту величину, и увидеть это было нечем.
"""
import pytest

from src import delivery


class _Поз:
    token_mint = "TOK"
    entry_price = 1.0
    entry_ts = 1000.0
    entry_mc = 1000.0
    entry_actors = ["a"]
    exited_actors = ["a"]
    peak_price = 2.0
    remaining = 1.0
    realized = 0.0


def _записи(monkeypatch):
    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    monkeypatch.setattr(delivery, "send_telegram", lambda t: True)
    return out


def test_фактический_итог_вытесняет_модельный(monkeypatch):
    """Цена трекера говорит +50%, деньги говорят +35% — правдой считаются деньги."""
    out = _записи(monkeypatch)
    delivery.deliver_exit(_Поз(), 1.5, "actors_exit", telegram=False,
                          realized_actual=0.35, exec_gap=-0.15)
    r = out[0]
    assert r["realized_pnl"] == pytest.approx(0.35)
    assert r["realized_model"] == pytest.approx(0.50)
    assert r["exec_gap"] == pytest.approx(-0.15)
    assert r["pnl_source"] == "деньги"


def test_комиссия_не_вычитается_дважды(monkeypatch):
    """В фактической выручке комиссии УЖЕ сидят — вычитать EXIT_FEE поверх нельзя."""
    out = _записи(monkeypatch)
    delivery.deliver_exit(_Поз(), 1.5, "actors_exit", telegram=False, realized_actual=0.35)
    assert out[0]["realized_net"] == pytest.approx(0.35)


def test_без_фактической_выручки_работает_как_раньше(monkeypatch):
    """Бумажный режим и живые сделки с непрочитанной выручкой — прежняя модель."""
    out = _записи(monkeypatch)
    delivery.deliver_exit(_Поз(), 1.5, "actors_exit", telegram=False)
    r = out[0]
    assert r["realized_pnl"] == pytest.approx(0.50)
    assert r["realized_model"] == pytest.approx(0.50)
    assert r["pnl_source"] == "модель"
    assert r["realized_net"] == pytest.approx(0.50 - delivery.EXIT_FEE)


def test_модельный_итог_сохраняется_всегда(monkeypatch):
    """Сравнение двух чисел — единственный способ видеть цену исполнения.
    Если оставить только факт, разрыв станет невидимым и вернётся тихо."""
    out = _записи(monkeypatch)
    delivery.deliver_exit(_Поз(), 1.5, "actors_exit", telegram=False,
                          realized_actual=0.35, exec_gap=-0.15)
    assert "realized_model" in out[0] and "exec_gap" in out[0]


def test_убыточный_выход_по_деньгам_при_плюсовой_модели(monkeypatch):
    """Девять сделок из 52 были именно такими: модель в плюс, деньги в минус."""
    out = _записи(monkeypatch)
    delivery.deliver_exit(_Поз(), 1.2, "actors_exit", telegram=False,
                          realized_actual=-0.05, exec_gap=-0.25)
    r = out[0]
    assert r["realized_model"] > 0 > r["realized_pnl"]
