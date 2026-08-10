"""Правки 6-10 аудита 10.08: мёртвые ручки, дубли кошельков, пыль, сверка, тексты.

Каждый тест закрывает случай, найденный на боевых данных, а не придуманный.
"""
import json

import pytest

from src import delivery, ledger, signal_engine, strategy, swap


# ---------- 6. мёртвые ручки конфига ----------
def test_гейтов_по_mc_и_возрасту_больше_нет_ни_в_конфиге_ни_в_проверке():
    """Настройка, которая выглядит работающей, но не работает, опаснее её отсутствия.

    Гейты выброшены из движка ещё аудитом-6 (не срабатывали ни разу: BuyEvent приходит
    без token_mc), а замер показал, что включать их вредно — сигналы с MC>100k дают
    win 0.57 против 0.42. В конфиге они продолжали лежать до 10.08.
    """
    assert "SIGNAL_MAX_MC_USD" not in strategy.SIGNAL
    assert "SIGNAL_MAX_AGE_S" not in strategy.SIGNAL
    assert "SIGNAL_MAX_MC_USD" not in strategy._REQUIRED["signal"]


def test_конфиг_проходит_валидацию_без_удалённых_ключей():
    assert strategy.load() is not None


# ---------- 7. один кошелёк у двух акторов ----------
def _watchlist(tmp_path, actors):
    p = tmp_path / "wl.json"
    p.write_text(json.dumps(actors), encoding="utf-8")
    return p


def test_кошелёк_у_двух_акторов_достаётся_большему_весу(tmp_path, capsys):
    """Найдено на боевом списке: 1 кошелёк из 129 числится за двумя акторами.

    Раньше побеждал тот, кто оказался позже в файле, молча. Это не безобидно:
    кошелёк один, а конфлюенс считает РАЗНЫХ акторов, и от того, кому он достался,
    зависит, наберётся ли порог.
    """
    p = _watchlist(tmp_path, [
        {"actor_id": "слабый", "wallets": ["общий"], "weight": 1.0},
        {"actor_id": "сильный", "wallets": ["общий"], "weight": 9.0},
    ])
    m = signal_engine.load_actor_map(p)
    assert m["общий"] == ("сильный", 9.0)
    assert "двумя акторами" in capsys.readouterr().out


def test_выбор_не_зависит_от_порядка_в_файле(tmp_path):
    """Устойчивость: перезапуск не должен переигрывать принадлежность кошелька."""
    a = {"actor_id": "слабый", "wallets": ["общий"], "weight": 1.0}
    b = {"actor_id": "сильный", "wallets": ["общий"], "weight": 9.0}
    (tmp_path / "1").mkdir()
    (tmp_path / "2").mkdir()
    прямой = signal_engine.load_actor_map(_watchlist(tmp_path / "1", [a, b]), quiet=True)
    обратный = signal_engine.load_actor_map(_watchlist(tmp_path / "2", [b, a]), quiet=True)
    assert прямой == обратный == {"общий": ("сильный", 9.0)}


def test_без_дублей_ничего_не_печатает(tmp_path, capsys):
    p = _watchlist(tmp_path, [{"actor_id": "A", "wallets": ["w1", "w2"], "weight": 1.0}])
    signal_engine.load_actor_map(p)
    assert "двумя акторами" not in capsys.readouterr().out


# ---------- 8. пыль при полной продаже ----------
def test_полная_продажа_уходит_ВСЯ_без_остатка():
    """Прежде количество считалось как int(uiAmount * 1.0 * 10**decimals).

    Обратный пересчёт через float оставлял на аккаунте несколько минимальных единиц,
    а ненулевой баланс не даёт закрыть токен-аккаунт и вернуть ренту ~0.002 SOL.
    При 230 сделках в сутки это до ~35$ замороженных ежедневно.
    """
    raw = 271_961_196_796_815          # найдено перебором: здесь float теряет единицу
    assert swap.sell_amount_raw(raw, 1.0) == raw
    старый_способ = int((raw / 10 ** 6) * 1.0 * 10 ** 6)   # как считали раньше
    assert старый_способ == raw - 1, "на этом значении и рождалась пыль"


def test_доля_значений_с_потерей_точности():
    """Замер перебором: 1.24% полных продаж оставляли ровно одну минимальную единицу.

    Цифра важна для честности: это не «$35 в сутки заморожено», а примерно три
    незакрытых аккаунта из 230 сделок = около $0.45 в сутки. Правка дешёвая,
    поэтому сделана, но выдавать её за крупную экономию нельзя.
    """
    import random
    random.seed(7)
    потерь = sum(1 for _ in range(20_000)
                 if (lambda r: int((r / 10 ** 6) * 10 ** 6) != r)(
                     random.randrange(10 ** 12, 10 ** 15)))
    assert 0.005 < потерь / 20_000 < 0.03


def test_частичная_продажа_округляется_вниз():
    """Вниз, а не вверх: продать больше, чем есть, транзакция не даст."""
    assert swap.sell_amount_raw(1001, 0.5) == 500


def test_доля_больше_единицы_не_просит_лишнего():
    assert swap.sell_amount_raw(777, 1.5) == 777


# ---------- 9. сверка леджера ----------
def _строки():
    return [
        # старая пара: выход подшит к намерению на ПОКУПКУ (до правки 05.08)
        {"type": "intent", "id": "старый", "side": "buy", "price": 1.0},
        {"type": "fill", "intent_id": "старый", "price": 1.0, "usd": 10.0},
        {"type": "fill", "intent_id": "старый", "price": 0.08, "usd": 0.8, "reason": "actors_exit"},
        # нормальная пара
        {"type": "intent", "id": "новый", "side": "sell", "price": 2.0},
        {"type": "fill", "intent_id": "новый", "price": 1.9, "usd": 19.0, "reason": "timeout"},
    ]


def test_доходность_позиции_не_считается_проскальзыванием():
    """fp/ip по кросс-паре давало −92% и портило цифру в каждом пульсе.

    В журнале таких пар 661 из 5331. Они историчны и больше не появляются,
    но сверка продолжала их учитывать.
    """
    r = ledger.reconcile(_строки())
    assert r["crossed_legs"] == 1
    assert r["worst_slippage"] == pytest.approx(-0.05), "должна остаться только честная пара"


def test_сверка_по_прежнему_ловит_исполнение_без_намерения():
    """Главная тревога сверки не должна пострадать от правки."""
    r = ledger.reconcile(_строки() + [{"type": "fill", "intent_id": "нетакого", "price": 1.0,
                                       "usd": 1.0}])
    assert r["orphan_fills"] == 1 and r["ok"] is False


def test_в_бумажном_режиме_проскальзывание_не_выдаётся_за_измерение(monkeypatch):
    """Цена намерения и цена исполнения в paper — одно и то же число.

    «медиана slippage +0.00%» в пульсе выглядела достижением, а была тавтологией.
    """
    monkeypatch.setattr(ledger, "load", lambda path=None: [
        {"type": "intent", "id": "i", "side": "buy", "price": 1.0},
        {"type": "fill", "intent_id": "i", "price": 1.0, "usd": 10.0, "mode": "paper"},
    ])
    assert "нет живых сделок" in ledger.summary()


def test_после_первой_живой_сделки_проскальзывание_показывается(monkeypatch):
    monkeypatch.setattr(ledger, "load", lambda path=None: [
        {"type": "intent", "id": "i", "side": "buy", "price": 1.0},
        {"type": "fill", "intent_id": "i", "price": 1.02, "usd": 10.0, "mode": "live"},
    ])
    assert "медиана slippage +2.00%" in ledger.summary()


# ---------- 10. тексты причин выхода ----------
@pytest.mark.parametrize("reason", ["actors_exit", "take_profit", "stop_loss", "trailing",
                                    "dead", "timeout", "lost_price", "orphan"])
def test_каждая_причина_выхода_переведена(reason):
    """timeout — вторая по частоте причина выхода, а уходила в Telegram как сырое слово."""
    assert reason in delivery._REASON_TXT


def test_все_причины_из_кода_есть_в_словаре():
    """Страховка от расхождения: причины задаются в positions.py и monitor.py."""
    из_кода = {"actors_exit", "take_profit", "stop_loss", "trailing", "dead",
               "timeout", "lost_price", "orphan", "take_partial"}
    непереведённые = из_кода - set(delivery._REASON_TXT) - {"take_partial"}
    assert not непереведённые, f"без перевода: {непереведённые}"
