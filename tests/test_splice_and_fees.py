"""Три дефекта учёта, найденных 11.08 при разборе чужого отчёта.

1. Монитор передавал МОДЕЛЬНЫЙ итог в параметр «фактические деньги»: бумажные записи
   уходили помеченными `pnl_source: "деньги"` и без вычета EXIT_FEE, то есть завышенными
   на 12% и выглядящими измеренными.
2. `load_closed` вычитал EXIT_FEE безусловно — на живых записях, где комиссии уже внутри
   полученной суммы, второй раз.
3. Траектории читались без склейки шкалы: на стыке кривая→DEX цена прыгает с медианой
   1.1464 при контроле ровно 1.0000 внутри источника (замер на 1.76M выборок).
"""
import pytest

from src import analysis


# ---------- склейка шкалы ----------

def test_склейка_убирает_скачок_на_стыке_кривая_декс():
    """Рынок не двигался, сменился только источник — ряд обязан остаться ровным."""
    точки = [(1, 1.0, "curve"), (2, 1.0, "curve"), (3, 1.15, "dex"), (4, 1.15, "dex")]
    из = analysis.склеить(точки)
    цены = [p for _, p, _ in из]
    assert цены == pytest.approx([1.0, 1.0, 1.0, 1.0])


def test_склейка_сохраняет_настоящее_движение_после_стыка():
    """Гасится только ступенька, но не рост, случившийся уже на DEX."""
    точки = [(1, 1.0, "curve"), (2, 1.15, "dex"), (3, 2.30, "dex")]
    цены = [p for _, p, _ in analysis.склеить(точки)]
    assert цены == pytest.approx([1.0, 1.0, 2.0])


def test_склейка_не_трогает_переход_от_якоря_сигнала():
    """`signal`→`curve` — это разрыв «цена актора против рынка» (медиана 0.9452),
    настоящая проблема исполнения. Склеить его значит спрятать её."""
    точки = [(1, 1.0, "signal"), (2, 0.9, "curve"), (3, 0.9, "curve")]
    цены = [p for _, p, _ in analysis.склеить(точки)]
    assert цены == pytest.approx([1.0, 0.9, 0.9])


def test_склейка_переживает_нулевую_цену():
    точки = [(1, 1.0, "curve"), (2, 0.0, "dex"), (3, 1.0, "dex")]
    analysis.склеить(точки)          # не должно бросить и не должно делить на ноль


def test_склейка_обратного_перехода_декс_кривая():
    """Обратный стык тоже бывает — токен уходит на DEX и возвращается на кривую."""
    точки = [(1, 1.15, "dex"), (2, 1.0, "curve")]
    цены = [p for _, p, _ in analysis.склеить(точки)]
    assert цены == pytest.approx([1.15, 1.15])


def test_траектории_склеивают_по_умолчанию(tmp_path):
    (tmp_path / "price_history.jsonl").write_text("\n".join([
        '{"ts": 1, "mint": "A", "price_usd": 1.0, "src": "curve"}',
        '{"ts": 2, "mint": "A", "price_usd": 1.5, "src": "dex"}',
    ]), encoding="utf-8")
    т = analysis.траектории({"A"}, directory=tmp_path)
    assert [p for _, p, _ in т["A"]] == pytest.approx([1.0, 1.0])
    сырые = analysis.траектории({"A"}, directory=tmp_path, splice=False)
    assert [p for _, p, _ in сырые["A"]] == pytest.approx([1.0, 1.5])


# ---------- комиссия и источник итога ----------

def _запись(pnl, источник, режим="live", ts="2026-08-11T10:00:00+00:00"):
    return {"type": "exit", "ts": ts, "entry_ts": 1000, "realized_pnl": pnl,
            "pnl_source": источник, "mode": режим, "token_mint": "A"}


def test_комиссия_не_вычитается_дважды_из_живого_итога(monkeypatch, tmp_path):
    """У записей по деньгам комиссии уже внутри суммы — второй вычет занижал бы
    живые сделки ровно на ту величину, вокруг которой идёт спор о доходности."""
    monkeypatch.setattr(analysis, "read_jsonl",
                        lambda имя, directory=None: [_запись(0.5, "деньги"),
                                                     _запись(0.5, "модель")])
    из = {r["pnl_source"]: r["pnl"] for r in analysis.load_closed()}
    from src import strategy
    fee = strategy.RISK["EXIT_FEE"]
    assert из["деньги"] == pytest.approx(0.5)
    assert из["модель"] == pytest.approx(0.5 - fee)


def test_бумажная_запись_с_ложной_меткой_деньги_всё_равно_платит_комиссию(monkeypatch):
    """До правки 11.08 монитор помечал КАЖДЫЙ бумажный выход как «деньги». Если верить
    одной метке, комиссия не вычтется из всего пласта бумажной истории с 10.08 —
    и починка одного дефекта тихо создаст другой. Решает пара «mode + источник»."""
    monkeypatch.setattr(analysis, "read_jsonl",
                        lambda имя, directory=None: [_запись(0.5, "деньги", "paper")])
    from src import strategy
    r = analysis.load_closed()[0]
    assert r["по_деньгам"] is False
    assert r["pnl"] == pytest.approx(0.5 - strategy.RISK["EXIT_FEE"])


def test_старые_записи_без_источника_считаются_моделью(monkeypatch):
    """Поле появилось позже; молча перестать вычитать комиссию из истории нельзя."""
    monkeypatch.setattr(analysis, "read_jsonl",
                        lambda имя, directory=None: [
                            {"type": "exit", "ts": "2026-08-01T00:00:00+00:00",
                             "entry_ts": 1, "realized_pnl": 0.5}])
    from src import strategy
    assert analysis.load_closed()[0]["pnl"] == pytest.approx(0.5 - strategy.RISK["EXIT_FEE"])


def test_бумажный_выход_помечается_моделью_и_теряет_комиссию(monkeypatch):
    """Главный дефект: в бумаге монитор обязан передать None, а не модельный итог."""
    from src import delivery, positions

    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    monkeypatch.setattr(delivery, "send_telegram", lambda *a, **k: None)
    pos = positions.Position(token_mint="A", entry_price=1.0, entry_ts=1.0,
                             entry_actors=["W"], entry_mc=1000.0, peak_price=1.5)
    delivery.deliver_exit(pos, 1.5, "timeout", telegram=False, realized_actual=None)
    rec = out[-1]
    from src import strategy
    assert rec["pnl_source"] == "модель"
    assert rec["realized_net"] == pytest.approx(rec["realized_pnl"] - strategy.RISK["EXIT_FEE"])


def test_живой_выход_помечается_деньгами_и_комиссию_не_теряет(monkeypatch):
    from src import delivery, positions

    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    monkeypatch.setattr(delivery, "send_telegram", lambda *a, **k: None)
    pos = positions.Position(token_mint="A", entry_price=1.0, entry_ts=1.0,
                             entry_actors=["W"], entry_mc=1000.0, peak_price=1.5)
    delivery.deliver_exit(pos, 1.5, "timeout", telegram=False, realized_actual=0.31)
    rec = out[-1]
    assert rec["pnl_source"] == "деньги"
    assert rec["realized_net"] == pytest.approx(0.31)


def test_отчёт_считает_живые_сделки_по_флагу_а_не_по_метке(monkeypatch, capsys):
    """Счёт по метке давал 1644 «денежных» сделки при 188 действительно живых."""
    from src import paper_eval
    monkeypatch.setattr(analysis, "read_jsonl",
                        lambda имя, directory=None: [_запись(0.1, "деньги", "paper"),
                                                     _запись(0.1, "деньги", "live")])
    monkeypatch.setattr(paper_eval, "_load", lambda имя: [])
    monkeypatch.setattr(paper_eval.config, "OUTPUT_DIR", __import__("pathlib").Path("/нет"))
    paper_eval.main()
    assert "итог по деньгам: 1 сделок" in capsys.readouterr().out
