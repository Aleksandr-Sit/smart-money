"""Слияние кандидатов discovery с проверенными акторами (аудит 09.08).

Автообновление ЗАМЕНЯЛО watchlist результатом discovery. Проверка первой же
свежей выгрузки на 8031 нашей сделке: замена потеряла бы 3831 сделку и 4153$,
потому что discovery отбирает по своим формальным признакам и не знает нашей
статистики. Оба лучших наших актора в выгрузку не попали, хотя активны.
"""
import time

import pytest

from src import merge_watchlist as mw


def _a(aid, wallets, **kw):
    return {"actor_id": aid, "wallets": list(wallets), "n_wallets": len(wallets),
            "weight": kw.pop("weight", 1.0), **kw}


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


def test_новому_актору_проставляется_дата_добавления(monkeypatch):
    monkeypatch.setattr(mw, "_observation_days", lambda: 30.0)
    monkeypatch.setattr(mw, "_seen_actors", lambda w2a: set())
    out, _ = mw.merge([_a("новый", ["wN"])], [])
    assert out[0]["source"] == "discovery"
    assert time.time() - out[0]["added_ts"] < 5
