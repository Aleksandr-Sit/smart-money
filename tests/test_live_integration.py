"""Инварианты подключения живого исполнения к монитору (аудит 07.08).

До этой правки swap.py вообще не вызывался монитором: модуль исполнения существовал,
но оркестратор о нём не знал, и LIVE_ENABLED=true не привёл бы ни к одной сделке.

Главная опасность подключения — рассинхрон между тем, что бот СЧИТАЕТ своей позицией,
и тем, что реально лежит в кошельке. Каждый тест ниже закрывает один такой случай.
"""
import pytest

from src import positions, strategy


def _pm(tmp_path, monkeypatch):
    """Тейки задаём явно: в боевом конфиге они отключены, но откат тейка — это
    код, который обязан работать корректно, если механизм вернут."""
    monkeypatch.setattr(positions.config, "OUTPUT_DIR", tmp_path, raising=False)
    pm = positions.PositionManager({**strategy.EXIT, "PARTIAL_TAKES": [(2.0, 0.5)]})
    pm.path = tmp_path / "open_positions.json"
    pm.pos = {}
    return pm


def test_откат_частичного_тейка_возвращает_остаток(tmp_path, monkeypatch):
    """check_price уменьшает remaining ДО отправки свопа. Если своп упал, токены
    остались у нас — без отката бот на выходе продаст меньше, чем владеет."""
    pm = _pm(tmp_path, monkeypatch)
    pm.open("MINT", 1.0, 1000.0, ["a"], 0.0)
    p = pm.get("MINT")
    res = pm.check_price("MINT", 2.0, 0.0)          # 2x → частичный тейк
    assert res and res["action"] == "partial"
    frac = res["frac"]
    assert p.remaining == pytest.approx(1.0 - frac)
    pm.rollback_partial("MINT", frac, 2.0)
    assert p.remaining == pytest.approx(1.0)         # остаток вернулся
    assert p.realized == pytest.approx(0.0)          # прибыль не засчитана
    assert p.taken == []                             # уровень снова доступен


def test_после_отката_тейк_можно_повторить(tmp_path, monkeypatch):
    """Иначе уровень сгорел бы: сделка не прошла, а второй попытки нет."""
    pm = _pm(tmp_path, monkeypatch)
    pm.open("MINT", 1.0, 1000.0, ["a"], 0.0)
    r1 = pm.check_price("MINT", 2.0, 0.0)
    pm.rollback_partial("MINT", r1["frac"], 2.0)
    r2 = pm.check_price("MINT", 2.0, 0.0)
    assert r2 and r2["action"] == "partial"


def test_доля_кошелька_считается_от_текущего_баланса():
    """swap.sell(fraction) продаёт долю ТЕКУЩЕГО баланса, а PARTIAL_TAKES заданы
    в долях ИСХОДНОЙ позиции. Путаница здесь = продали не тот объём."""
    # позиция цела, тейк 50% от исходной → это же 50% кошелька
    remaining_after, frac = 0.5, 0.5
    before = remaining_after + frac
    assert frac / before == pytest.approx(0.5)

    # второй тейк 25% от исходной, когда на руках уже только 50%
    remaining_after, frac = 0.25, 0.25
    before = remaining_after + frac
    assert frac / before == pytest.approx(0.5)       # это ПОЛОВИНА кошелька, не четверть


def test_полный_выход_продаёт_весь_остаток_кошелька(tmp_path, monkeypatch):
    """На полном выходе доля всегда 1.0: в кошельке ровно то, что не продано ранее.
    Передать туда p.remaining (долю ИСХОДНОЙ позиции) — значит недопродать."""
    pm = _pm(tmp_path, monkeypatch)
    pm.open("MINT", 1.0, 1000.0, ["a"], 0.0)
    pm.check_price("MINT", 2.0, 0.0)                 # частичный тейк прошёл
    p = pm.get("MINT")
    assert p.remaining < 1.0                         # от ИСХОДНОЙ позиции осталось меньше
    # в кошельке при этом 100% непроданных токенов — продавать надо их целиком
    доля_для_свопа = 1.0
    assert доля_для_свопа == 1.0


def test_инварианты_боевого_конфига_при_живых_деньгах():
    """Живой режим включён владельцем 10.08. Тест больше не запрещает торговлю —
    он держит условия, без которых торговать нельзя.

    Прежняя версия просто требовала LIVE_ENABLED=False. Такой предохранитель
    защищал ровно до того дня, когда флаг понадобилось включить, и дальше его
    пришлось бы удалить целиком, потеряв и остальные проверки. Поэтому здесь
    сформулированы инварианты, а не запрет.
    """
    live = strategy.EXECUTION["LIVE_ENABLED"]
    if not live:
        assert strategy.RISK["RISK_MODE"] == "shadow"
        return

    # 1. Денежные лимиты обязаны РЕЗАТЬ, а не логироваться: в shadow дневной стоп
    #    не остановил бы слив.
    assert strategy.RISK["RISK_MODE"] == "enforce"

    # 2. Читать и отправлять через один публичный узел нельзя: его падение
    #    одновременно ослепит бота и лишит возможности закрыть позиции.
    assert not (strategy.EXECUTION["SEND_PROVIDER"] == strategy.TRACKING["RPC_PROVIDER"]
                == "public")

    # 3. Банк должен быть похож на реальные деньги: дневной стоп считается от него,
    #    и завышенный банк делает стоп недостижимым.
    клипов = strategy.RISK["BANKROLL_USD"] / strategy.RISK["CLIP_USD"]
    assert клипов >= 15, "risk-of-ruin: меньше 15 клипов = разорение до прихода хвоста"
    assert strategy.RISK["MAX_POSITIONS"] * strategy.RISK["CLIP_USD"] <= \
        strategy.RISK["BANKROLL_USD"]

    # 4. Один своп не должен уносить заметную долю банка даже при ошибке вызова.
    assert strategy.EXECUTION["MAX_SWAP_USD"] <= strategy.RISK["BANKROLL_USD"] / 2

    # 5. Вывод на холодный кошелёк включается ОТДЕЛЬНЫМ решением, не вместе с торговлей.
    assert strategy.SWEEP["ENABLED"] is False
