"""Замер достижимой цены — тесты на поведение.

Окно 0–15с после сигнала невидимо: трекер снимает цену раз в 15 секунд. Именно в
нём решается вопрос лаунч-снайпа (вход по цене актора +39.7%, на следующем тике
−13.5%). Тесты закрепляют, что замер не мешает торговле и честно переживает отказы.
"""
import asyncio
import json

import pytest

from src import reachable as rc


@pytest.fixture
def журнал(tmp_path, monkeypatch):
    monkeypatch.setattr(rc.config, "OUTPUT_DIR", tmp_path)
    return tmp_path / rc.ФАЙЛ


def _прогнать(корутина):
    return asyncio.new_event_loop().run_until_complete(корутина)


def test_замер_пишет_обе_задержки(журнал, monkeypatch):
    monkeypatch.setattr(rc, "цена_кривой", lambda m: 2.0)
    rec = _прогнать(rc.замерить("TOK", 1.0, задержки=(0.0, 0.0), независимых=1))
    assert set(rec["замеры"]) == {"0.0"}
    d = [json.loads(x) for x in журнал.read_text(encoding="utf-8").splitlines()]
    assert d[0]["mint"] == "TOK" and d[0]["независимых"] == 1


def test_отказ_узла_не_роняет_замер(журнал, monkeypatch):
    """Цена может не прочитаться — строка обязана появиться с None, а не пропасть.
    Пропавшая строка молча смещала бы выборку в сторону удачных чтений."""
    monkeypatch.setattr(rc, "цена_кривой", lambda m: None)
    rec = _прогнать(rc.замерить("TOK", 1.0, задержки=(0.0,)))
    assert rec["замеры"]["0.0"] is None
    assert журнал.exists()


def test_цена_кривой_гасит_исключение(monkeypatch):
    """Путь замера не имеет права поднять исключение в торговый цикл."""
    monkeypatch.setattr(rc.price_track, "bonding_curve_pda",
                        lambda m: (_ for _ in ()).throw(RuntimeError("узел лёг")))
    assert rc.цена_кривой("TOK") is None


def test_запустить_без_цикла_событий_не_падает(monkeypatch):
    """`запустить` зовётся из синхронного участка обработки сигнала."""
    monkeypatch.setattr(rc.asyncio, "get_event_loop",
                        lambda: (_ for _ in ()).throw(RuntimeError("нет цикла")))
    rc.запустить("TOK", 1.0)          # не должно бросить


def test_сбор_считает_множители_от_кривой_а_не_от_цены_сигнала(журнал):
    """База — замер кривой на +0с. Цена сигнала приходит из другого источника
    (DexScreener либо он-чейн сделка), и сравнение с ней мерило бы расхождение
    источников пополам с движением цены: первые 19 замеров дали 0.838x."""
    журнал.write_text("\n".join(json.dumps(r) for r in [
        {"цена_сигнала": 9.9, "замеры": {"0.0": 1.0, "2.0": 1.2, "5.0": 1.5}},
        {"цена_сигнала": 9.9, "замеры": {"0.0": 2.0, "2.0": 2.4, "5.0": 3.0}},
        {"цена_сигнала": 9.9, "замеры": {"0.0": 1.0, "2.0": 1.4, "5.0": None}},
    ]), encoding="utf-8")
    d = rc.собрать(журнал)
    assert d["по_задержкам"]["2.0"]["n"] == 3
    assert d["по_задержкам"]["2.0"]["медиана"] == pytest.approx(1.2)
    assert d["по_задержкам"]["5.0"]["n"] == 2, "None не должен считаться замером"
    assert "0.0" not in d["по_задержкам"], "база не сравнивается сама с собой"


def test_замер_без_базы_не_учитывается(журнал):
    """Если кривая на +0с не прочиталась, множитель считать не от чего."""
    журнал.write_text(json.dumps({"цена_сигнала": 1.0, "замеры": {"0.0": None, "2.0": 1.4}}),
                      encoding="utf-8")
    assert rc.собрать(журнал)["по_задержкам"] == {}


def test_расхождение_источников_считается_отдельно(журнал):
    журнал.write_text(json.dumps({"цена_сигнала": 2.0, "замеры": {"0.0": 1.0, "2.0": 1.0}}),
                      encoding="utf-8")
    assert rc.собрать(журнал)["источник"]["медиана"] == pytest.approx(0.5)


def test_нулевая_цена_сигнала_не_даёт_деления_на_ноль(журнал):
    журнал.write_text(json.dumps({"цена_сигнала": 0, "замеры": {"0.0": 0, "2.0": 1.0}}),
                      encoding="utf-8")
    assert rc.собрать(журнал)["по_задержкам"] == {}


def test_отчёт_признаёт_отсутствие_данных(журнал):
    assert "замеров пока нет" in rc.отчёт(rc.собрать(журнал))


def test_отчёт_называет_разрыв_входа(журнал):
    журнал.write_text(json.dumps({"цена_сигнала": 1.0, "замеры": {"0.0": 1.0, "2.0": 1.4}}),
                      encoding="utf-8")
    т = rc.отчёт(rc.собрать(журнал))
    assert "1.400x" in т and "дороже актора" in т


def test_причина_отказа_записывается(monkeypatch):
    """Первый прогон дал сплошные null, и по журналу было не понять почему.
    Молчаливый None — тот же класс дефекта, что стоил первой живой покупки."""
    rc.СБОИ.clear()
    monkeypatch.setattr(rc.price_track, "bonding_curve_pda", lambda m: "PDA")
    monkeypatch.setattr(rc.helius, "rpc",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    assert rc.цена_кривой("TOK") is None
    assert rc.СБОИ["RuntimeError"] == 1
    assert "RuntimeError" in rc.сбои_текстом()


def test_повтор_спасает_разовый_отказ_узла(monkeypatch):
    """Публичный узел отвечает 429 под нагрузкой — одна повторная попытка."""
    rc.СБОИ.clear()
    звонки = {"n": 0}

    def rpc(*a, **k):
        звонки["n"] += 1
        if звонки["n"] == 1:
            raise RuntimeError("429")
        return {"result": {"value": {"data": ["AA=="]}}}

    monkeypatch.setattr(rc.price_track, "bonding_curve_pda", lambda m: "PDA")
    monkeypatch.setattr(rc.helius, "rpc", rpc)
    monkeypatch.setattr(rc.price_track, "parse_curve",
                        lambda b: {"price_sol": 2.0, "complete": False})
    monkeypatch.setattr(rc.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(rc.time, "sleep", lambda s: None)
    assert rc.цена_кривой("TOK") == pytest.approx(200.0)
    assert звонки["n"] == 2


def test_закрытая_кривая_отличается_от_отказа(monkeypatch):
    """Грэдуировавший токен — не сбой узла, и в счётчике это разные строки."""
    rc.СБОИ.clear()
    monkeypatch.setattr(rc.price_track, "bonding_curve_pda", lambda m: "PDA")
    monkeypatch.setattr(rc.helius, "rpc", lambda *a, **k: {"result": {"value": {"data": ["AA=="]}}})
    monkeypatch.setattr(rc.price_track, "parse_curve", lambda b: {"price_sol": 1.0, "complete": True})
    assert rc.цена_кривой("TOK") is None
    assert rc.СБОИ["кривая закрыта (грэдуэйшн)"] == 1
