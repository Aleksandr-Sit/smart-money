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


def test_разрыв_показан_долей_а_не_разницей():
    """Разбор 13.08: модель +359%, деньги +7%. Вычитание давало «разрыв −352%» —
    число без смысла для сделки на $10. Отношение даёт 0.23 и читается."""
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", 0.073,
                             модель=3.593, разрыв=-3.520, разрыв_к=1.073 / 4.593)
    assert "до нас дошло 23% от модели" in т
    assert "-352%" not in т and "−352%" not in т


def test_старая_форма_остаётся_когда_коэффициента_нет():
    """Записи до правки коэффициента не имеют — сообщение обязано остаться читаемым."""
    т = delivery.format_exit(_поз(), 8.39e-06, "actors_exit", -0.3966,
                             модель=0.2018, разрыв=-0.5983)
    assert "РАЗРЫВ ИСПОЛНЕНИЯ -60%" in т


def test_запись_хранит_коэффициент_и_время_удержания(monkeypatch):
    """`held_s` не писался ни в одну из 178 живых записей — разбор восстанавливал
    время вычитанием и терял записи с битым `ts`."""
    out = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: out.append(o))
    monkeypatch.setattr(delivery, "time", type("t", (), {"time": staticmethod(lambda: 1077.0)}))
    delivery.deliver_exit(_поз(), 8.39e-06, "actors_exit", telegram=False,
                          realized_actual=-0.3966, exec_gap=-0.5983, разрыв_к=0.891)
    rec = out[-1]
    assert rec["разрыв_к"] == pytest.approx(0.891)
    assert rec["held_s"] == pytest.approx(77.0)


def test_невозможный_убыток_по_деньгам_не_попадает_в_разбор(tmp_path):
    """Две записи от 10.08 (−200.1% и −102.7%) давали 19% всего живого убытка.
    Спот-сделка не может потерять больше вложенного — это сломанный замер."""
    import json

    from src import analysis
    строки = [
        {"type": "exit", "ts": "2026-08-10T14:52:49+00:00", "entry_ts": 1000.0,
         "realized_pnl": -2.001, "pnl_source": "деньги", "mode": "live", "token_mint": "A"},
        {"type": "exit", "ts": "2026-08-10T15:00:47+00:00", "entry_ts": 1000.0,
         "realized_pnl": -0.30, "pnl_source": "деньги", "mode": "live", "token_mint": "B"},
    ]
    (tmp_path / "paper_closed.jsonl").write_text(
        "\n".join(json.dumps(s, ensure_ascii=False) for s in строки), encoding="utf-8")
    оставшиеся = analysis.load_closed(tmp_path)
    assert [r["token_mint"] for r in оставшиеся] == ["B"]
