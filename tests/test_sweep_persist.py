"""Свип: отметка времени переживает перезапуск, а статус не врёт про баланс.

Оба дефекта висели с момента написания модуля и всплыли при подготовке к возврату
живых денег. Свип пока выключен (SWEEP.ENABLED=false), поэтому чинится заранее.
"""
import json
import time

import pytest

from src import sweep


@pytest.fixture(autouse=True)
def песочница(tmp_path, monkeypatch):
    monkeypatch.setattr(sweep.config, "OUTPUT_DIR", tmp_path)
    return tmp_path


def test_отметка_времени_переживает_перезапуск(песочница):
    """Переменная в памяти обнулялась каждым `up --build`, а деплой случается чаще
    MIN_INTERVAL_S — защита от частых выводов существовала только на бумаге."""
    assert sweep._прочитать_последний() == 0.0
    t = time.time()
    sweep._записать_последний(t)
    assert sweep._прочитать_последний() == pytest.approx(t)
    assert json.loads((песочница / sweep._ФАЙЛ_ПОСЛЕДНЕГО).read_text(encoding="utf-8"))["ts"]


def test_битая_отметка_не_разрешает_немедленный_вывод(песочница):
    """Нечитаемый файл обязан читаться как «выводов не было», но интервал всё равно
    проверяется от нуля — то есть первый вывод разрешён, а второй уже нет."""
    (песочница / sweep._ФАЙЛ_ПОСЛЕДНЕГО).write_text("{битый", encoding="utf-8")
    assert sweep._прочитать_последний() == 0.0


def test_интервал_считается_из_файла(monkeypatch, песочница):
    """Главная проверка: после «перезапуска» (новый импорт состояния нет) свежий
    вывод по-прежнему блокируется интервалом."""
    sweep._записать_последний(time.time())
    monkeypatch.setitem(sweep.strategy.SWEEP, "ENABLED", True)
    monkeypatch.setitem(sweep.strategy.SWEEP, "MIN_INTERVAL_S", 3600)

    class _W:
        available = True

    monkeypatch.setattr(sweep.wallet, "Wallet", lambda: _W())
    r = sweep.execute(dry_run=True)
    assert r["action"] == "skip" and "интервал" in r["reason"]


def test_статус_показывает_и_SOL_и_доллары(monkeypatch, песочница):
    """Кошелёк номинирован в SOL, пороги в долларах: при движении курса баланс
    пересекает порог без единой сделки, и в долларах это выглядит как заработок."""
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(sweep, "plan", lambda *a, **k: {"action": "skip", "reason": "тест",
                                                       "balance_usd": 250.0})
    monkeypatch.setattr(sweep, "destination", lambda: "CoLdWaLLeT000000000000000000000000000000000")
    т = sweep.status()
    assert "2.5000 SOL" in т and "$250.00" in т


def test_статус_говорит_когда_выводов_не_было(monkeypatch, песочница):
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(sweep, "plan", lambda *a, **k: {"action": "skip", "reason": "тест",
                                                       "balance_usd": 10.0})
    monkeypatch.setattr(sweep, "destination", lambda: "CoLdWaLLeT000000000000000000000000000000000")
    assert "выводов не было" in sweep.status()
