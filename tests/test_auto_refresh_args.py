"""Аргументы автообновления должны существовать у вызываемых модулей.

Найдено 10.08 разбором первого автозапуска: auto_refresh звал
`universe --since`, а у universe такого ключа нет — есть --launch-start и
--launch-end. Первый же шаг падал с «unrecognized arguments», то есть
еженедельное обновление watchlist не могло отработать НИ РАЗУ. Откат сработал
и список уцелел, но функция была мертва, а выглядела рабочей.

Класс ошибки тот же, что мёртвые ручки в конфиге: код, который выглядит
работающим и не работает, опаснее его отсутствия. Тест ловит это статически —
разбирает парсер каждого модуля и проверяет, что переданные флаги там есть.
"""
import contextlib
import io
import re
from datetime import datetime, timedelta, timezone

import pytest


def _флаги_модуля(имя: str) -> set[str]:
    """Все длинные ключи парсера модуля — читаем из его же --help."""
    mod = __import__(f"src.{имя}", fromlist=["main"])
    буфер = io.StringIO()
    with contextlib.redirect_stdout(буфер), pytest.raises(SystemExit):
        import sys
        старый = sys.argv
        sys.argv = [имя, "--help"]
        try:
            mod.main()
        finally:
            sys.argv = старый
    return set(re.findall(r"--[a-z][a-z0-9-]*", буфер.getvalue()))


def _шаги():
    """Ровно то, что вызывает auto_refresh.main — держим синхронно с ним."""
    from src import auto_refresh
    now = datetime.now(timezone.utc)
    since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
    until = (now - timedelta(days=auto_refresh.MATURITY_DAYS)).strftime("%Y-%m-%d")
    return (("universe", ["--launch-start", since, "--launch-end", until, "--append"]),
            ("aggregate", ["--since", since]),
            ("flow_watchlist", []))


@pytest.mark.parametrize("модуль,аргументы", _шаги())
def test_каждый_флаг_существует(модуль, аргументы):
    есть = _флаги_модуля(модуль)
    переданы = [a for a in аргументы if a.startswith("--")]
    нет = [a for a in переданы if a not in есть]
    assert not нет, f"{модуль} не знает флагов {нет}; известные: {sorted(есть)}"


def test_universe_не_принимает_since():
    """Прямая фиксация исходной ошибки, чтобы её нельзя было вернуть по памяти."""
    assert "--since" not in _флаги_модуля("universe")
    assert "--launch-start" in _флаги_модуля("universe")


def test_aggregate_получает_окно_а_не_свой_дефолт():
    """Второй дефект того же места: aggregate звался вообще без --since и брал
    зашитый в него дефолт от 26 июня, то есть окно не двигалось со временем."""
    шаги = dict((m, a) for m, a in _шаги())
    assert "--since" in шаги["aggregate"]


def test_верхняя_граница_окна_отстаёт_на_срок_созревания():
    """Токену нужно время, чтобы стало видно, вырос он или умер. Окно до «сегодня»
    затащило бы в когорту незрелые токены и испортило разметку."""
    from src import auto_refresh
    шаги = dict((m, a) for m, a in _шаги())
    u = шаги["universe"]
    начало = datetime.strptime(u[u.index("--launch-start") + 1], "%Y-%m-%d")
    конец = datetime.strptime(u[u.index("--launch-end") + 1], "%Y-%m-%d")
    assert начало < конец, "окно должно быть непустым"
    отставание = (datetime.now(timezone.utc).replace(tzinfo=None) - конец).days
    assert отставание >= auto_refresh.MATURITY_DAYS - 1


def test_провал_шага_печатается_в_stdout(monkeypatch, capsys):
    """Причина отказа уходила ТОЛЬКО в Telegram, а крон-скрипт логирует stdout —
    поэтому лог провала был пуст, и три недели было не видно, что мешает.
    Настоящей причиной оказалась блокировка DuckDB живым контейнером discovery."""
    import sys
    from src import auto_refresh as ar

    monkeypatch.setattr(ar, "credits_left", lambda: 9999)
    monkeypatch.setattr(ar, "_run", lambda mod, margs: (False, "IO Error: Conflicting lock"))
    monkeypatch.setattr(ar.delivery, "send_alert", lambda *a, **k: None)
    monkeypatch.setattr(ar.delivery, "send_telegram", lambda *a, **k: None)
    monkeypatch.setattr(ar, "_load", lambda p: {})
    monkeypatch.setattr(sys, "argv", ["x", "--days", "30"])
    ar.main()
    вывод = capsys.readouterr().out
    assert "ПРОВАЛ на шаге universe" in вывод
    assert "Conflicting lock" in вывод, "текст ошибки обязан быть в логе, а не только в Telegram"
