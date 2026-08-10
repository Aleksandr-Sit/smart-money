"""Допуск проскальзывания: вход и выход — разные решения (10.08, первые 43 живые сделки).

Замер: 9 отказов из 43 = 21%, ВСЕ с ошибкой Jupiter Custom 6001 (SlippageToleranceExceeded).
По сторонам: покупки 6 из 24 = 25%, продажи 3 из 19 = 16%. Проскальзывание на ПРОШЕДШИХ
сделках: медиана 0.67%, p90 3.03% — допуск стоял ровно на краю распределения и срезал хвост,
причём сами замеры этим и цензурированы: всё, что вышло за 3%, до леджера не дошло.

Асимметрия, ради которой всё разделено: допуск — это ПОТОЛОК, а не издержка. Платим
фактическое проскальзывание, порог лишь решает, исполнять или отказать.
  сорванная ПОКУПКА  = комиссия ~$0.01–0.03 и пропущенная сделка. Дёшево.
  сорванная ПРОДАЖА  = позиция остаётся открытой, пока токен льют. Дорого.
"""
import pytest

from src import strategy, swap


def test_допуск_выхода_шире_допуска_входа():
    """Главный инвариант политики. Если равны — асимметрия потеряна."""
    вход = strategy.EXECUTION["SLIPPAGE_BPS"]
    выход = strategy.EXECUTION["SLIPPAGE_BPS_SELL"]
    assert выход > вход, "сорванный выход дороже сорванного входа"
    assert вход <= 500, "вход не должен быть щедрым: пропущенная покупка стоит копейки"
    assert выход <= 3000, "выход не должен быть безграничным — это уже не защита, а сдача"


def test_продажа_котируется_с_допуском_выхода(monkeypatch):
    видели = []
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a"})())
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (1_000_000, 6, "ata"))
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", False)

    def _quote(inp, out, amt, bps=None):
        видели.append(bps)
        return {"outAmount": "1000000"}

    monkeypatch.setattr(swap, "_quote", _quote)
    swap.sell("MINT", 1.0)
    assert видели == [strategy.EXECUTION["SLIPPAGE_BPS_SELL"]]


def test_покупка_котируется_с_допуском_входа(monkeypatch):
    видели = []
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a"})())
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 6, None))
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", False)

    def _quote(inp, out, amt, bps=None):
        видели.append(bps)
        return {"outAmount": "1000000"}

    monkeypatch.setattr(swap, "_quote", _quote)
    swap.buy("MINT", 10.0)
    # покупка зовёт _quote без явного bps → берётся SLIPPAGE_BPS входа
    assert видели == [None]


def test_запасной_вариант_если_ключа_нет(monkeypatch):
    """Старый конфиг без SLIPPAGE_BPS_SELL не должен ронять торговлю."""
    monkeypatch.delitem(swap.strategy.EXECUTION, "SLIPPAGE_BPS_SELL", raising=False)
    assert swap._sell_slippage_bps() == strategy.EXECUTION["SLIPPAGE_BPS"]


# ---------- диагноз отказа ----------
def test_ошибка_проскальзывания_объясняется_словами():
    """Алерт «не подтвердилась за таймаут» вводил в заблуждение: транзакция ДОЛЕТЕЛА
    и упала с определённой ошибкой. Это разные ситуации с разными последствиями."""
    txt = swap._объяснить({"InstructionError": [3, {"Custom": 6001}]})
    assert "проскальзыван" in txt and "деньги целы" in txt


def test_неизвестная_ошибка_передаётся_как_есть():
    assert "НечтоНовое" in swap._объяснить({"InstructionError": [1, "НечтоНовое"]})


def test_отклонение_сетью_отличается_от_таймаута(monkeypatch):
    """Транзакция упала на цепи → «ОТКЛОНЕНА сетью», сироты не будет."""
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a",
                                               "keypair": lambda s: "kp"})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap, "_quote", lambda *a, **k: {"outAmount": "1000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm_detail",
                        lambda s, t=None: (False, "превышен допуск проскальзывания"))
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 6, None))
    monkeypatch.setattr(swap, "_settled_token_balance_raw", lambda m, b, **k: (0, 6))
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill", lambda *a, **k: None)
    with pytest.raises(swap.SwapError, match="ОТКЛОНЕНА сетью"):
        swap.buy("MINT", 10.0)


def test_настоящий_таймаут_по_прежнему_помечен_как_таймаут(monkeypatch):
    """Причины нет → неизвестность → предупреждаем про возможного сироту."""
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a",
                                               "keypair": lambda s: "kp"})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap, "_quote", lambda *a, **k: {"outAmount": "1000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm_detail", lambda s, t=None: (False, None))
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (0, 6, None))
    monkeypatch.setattr(swap, "_settled_token_balance_raw", lambda m, b, **k: (0, 6))
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill", lambda *a, **k: None)
    with pytest.raises(swap.SwapError, match="разбор сирот"):
        swap.buy("MINT", 10.0)


def test_confirm_спрашивает_оба_узла(monkeypatch):
    """Узел чтения отстаёт на слоты; узел отправки транзакцию видел заведомо."""
    видели = []

    def _rpc(method, params, timeout=20, url=None):
        видели.append(url)
        if url is None:
            return {"result": {"value": [None]}}          # узел чтения ещё не знает
        return {"result": {"value": [{"confirmationStatus": "confirmed", "err": None}]}}

    monkeypatch.setattr(swap.helius, "rpc", _rpc)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    ok, причина = swap.confirm_detail("SIG", timeout_s=5)
    assert ok is True and причина is None
    assert "https://send" in видели
