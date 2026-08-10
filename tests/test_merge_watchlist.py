"""Слияние кандидатов discovery с проверенными акторами (аудит 09.08).

Автообновление ЗАМЕНЯЛО watchlist результатом discovery. Проверка первой же
свежей выгрузки на 8031 нашей сделке: замена потеряла бы 3831 сделку и 4153$,
потому что discovery отбирает по своим формальным признакам и не знает нашей
статистики. Оба лучших наших актора в выгрузку не попали, хотя активны.
"""
import time

import pytest

from src import merge_watchlist as mw

# настоящая функция, захваченная ДО автоподмены в фикстуре no_buy_journal
_настоящий_buy_activity = mw.buy_activity


def _a(aid, wallets, **kw):
    return {"actor_id": aid, "wallets": list(wallets), "n_wallets": len(wallets),
            "weight": kw.pop("weight", 1.0), **kw}


@pytest.fixture(autouse=True)
def no_buy_journal(monkeypatch):
    """Журнал покупок появился 10.08 — в большинстве тестов он пуст."""
    monkeypatch.setattr(mw, "buy_activity", lambda: {})


@pytest.fixture
def no_signals(monkeypatch):
    """По умолчанию: наблюдение долгое, сигналов не видел никто."""
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: set())


def test_проверенный_актор_переживает_ротацию(monkeypatch):
    """Главное свойство: discovery ПРЕДЛАГАЕТ, а не заменяет."""
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: {"старый"})
    out, rep = mw.merge([_a("новый", ["wN"])], [_a("старый", ["wS"])])
    ids = {a["actor_id"] for a in out}
    assert ids == {"старый", "новый"}
    assert rep["добавлено_новых"] == 1 and rep["выброшено_молчунов"] == 0


def test_молчун_с_подтверждённым_молчанием_выбрасывается(no_signals):
    """Ни одного сигнала за 30 суток наблюдения — факт, а не прогноз."""
    old = [_a("молчун", ["w1", "w2"], added_ts=time.time() - 60 * 86400)]
    out, rep = mw.merge([], old)
    assert out == [] and rep["выброшено_молчунов"] == 1


def test_новичок_защищён_испытательным_сроком(no_signals):
    """Актор, добавленный вчера, молчал не потому что плох — его не слушали.
    Без этой проверки первый холостой прогон пометил на выброс все 14 акторов,
    добавленных в тот же день."""
    old = [_a("новичок", ["w1"], added_ts=time.time() - 86400)]
    out, rep = mw.merge([], old)
    assert {a["actor_id"] for a in out} == {"новичок"}
    assert rep["выброшено_молчунов"] == 0 and rep["на_испытательном"] == 1


def test_испытательный_срок_истекает(no_signals):
    old = [_a("отсидел", ["w1"], added_ts=time.time() - 30 * 86400)]
    out, _ = mw.merge([], old, grace_days=14.0)
    assert out == []


def test_мало_наблюдений_никого_не_трогаем(monkeypatch):
    """Свежий сервер без истории не должен выкосить весь список."""
    monkeypatch.setattr(mw, "_observation_days", lambda: 3.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: set())
    old = [_a("A", ["w1"]), _a("B", ["w2"])]
    out, rep = mw.merge([], old, min_days=14.0)
    assert len(out) == 2 and rep["порог_не_достигнут"] is True


def test_кошельки_известного_актора_дополняются(monkeypatch):
    """Discovery нашёл новый кошелёк того же актора — добавляем, не теряя старые."""
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: {"A"})
    out, _ = mw.merge([_a("A", ["w2", "w3"])], [_a("A", ["w1", "w2"])])
    assert sorted(out[0]["wallets"]) == ["w1", "w2", "w3"]
    assert out[0]["source"] == "both"


def test_повторный_запуск_ничего_не_меняет(monkeypatch):
    """Идемпотентность: слияние поверх собственного результата — тот же список."""
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: {"старый"})
    cand = [_a("новый", ["wN"])]
    first, _ = mw.merge(cand, [_a("старый", ["wS"])])
    second, rep = mw.merge(cand, first)
    assert {a["actor_id"] for a in first} == {a["actor_id"] for a in second}
    assert rep["выброшено_молчунов"] == 0 and rep["добавлено_новых"] == 0


def test_срок_наблюдения_не_обнуляется_записью_выхода(monkeypatch):
    """signals.log хранит И сигналы, И выходы (deliver_exit пишет туда же).

    Дефект, найденный аудитом 10.08: срок наблюдения брался из ПЕРВОЙ и ПОСЛЕДНЕЙ
    строки файла. Последняя строка почти всегда запись выхода — ключа "signal" в ней
    нет, отметка времени не набиралась, срок выходил 0 суток, порог min_days не
    достигался, и ветка исключения молчунов не выполнялась НИ РАЗУ.
    """
    day = 86400
    журнал = [
        {"ts": "x", "signal": {"ts": 1_000_000.0, "actors": ["A"]}},
        {"ts": "x", "type": "exit", "token_mint": "T"},          # выход, не сигнал
        {"ts": "x", "signal": {"ts": 1_000_000.0 + 30 * day, "actors": ["B"]}},
        {"ts": "x", "type": "exit", "token_mint": "T2"},          # ПОСЛЕДНЯЯ строка — выход
    ]
    monkeypatch.setattr(mw.analysis, "read_jsonl", lambda *a, **k: журнал)
    seen, days = mw._scan_signals({})
    assert days == pytest.approx(30.0), "выход в конце файла не должен обнулять срок"
    assert seen == {"A", "B"}
    # и сквозь merge: молчун теперь действительно выбрасывается
    out, rep = mw.merge([], [_a("молчун", ["w1"], added_ts=time.time() - 60 * day)])
    assert rep["порог_не_достигнут"] is False and rep["выброшено_молчунов"] == 1
    assert out == []


def test_покупает_но_не_сходится_это_не_молчун(monkeypatch):
    """Актор жив и активен, но его покупки не совпадают по времени с чужими.

    Исключать автоматически нельзя: возможно, ушёл его НАПАРНИК, а не он сам.
    Поэтому категория отдельная и попадает в отчёт человеку, а решение о
    выбрасывании по-прежнему принимается только по молчанию в сигналах.
    """
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: {"одиночка"})
    monkeypatch.setattr(mw, "buy_activity",
                        lambda: {"одиночка": {"buys": 40, "converged": 0, "last_ts": 1.0}})
    out, rep = mw.merge([], [_a("одиночка", ["w1"])])
    assert len(out) == 1, "актор остаётся в списке"
    assert rep["покупают_но_не_сходятся"] == 1
    assert rep["выброшено_молчунов"] == 0


def test_журнал_покупок_считает_по_акторам(monkeypatch):
    записи = [
        {"ts": 10.0, "actor": "A", "converged": True},
        {"ts": 20.0, "actor": "A", "converged": False},
        {"ts": 30.0, "actor": "B", "converged": False},
        {"actor": None},                       # битая строка — пропустить, не упасть
    ]
    monkeypatch.setattr(mw.analysis, "read_jsonl", lambda *a, **k: записи)
    st = _настоящий_buy_activity()
    assert st["A"] == {"buys": 2, "converged": 1, "last_ts": 20.0}
    assert st["B"]["converged"] == 0


def test_новому_актору_проставляется_дата_добавления(monkeypatch):
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: set())
    out, _ = mw.merge([_a("новый", ["wN"])], [])
    assert out[0]["source"] == "discovery"
    assert time.time() - out[0]["added_ts"] < 5
