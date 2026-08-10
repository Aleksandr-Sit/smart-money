"""Уроки ПЕРВОЙ живой сделки (10.08) и подготовка к работе не в полную силу.

Пробная покупка USDC на $10 прошла на цепи (подпись 4zEM8vRK…, слот 438365865,
err=None, 10.004341 USDC в кошельке), но код объявил её неудачной: публичный узел
не отдал баланс, а `token_balance_raw` превращал ЛЮБУЮ ошибку RPC в «аккаунта нет».
Тихий ноль на денежном пути неотличим от правды и стоил нам первой живой сироты.
"""
import pytest

from src import delivery, helius, strategy, swap


# ---------- ошибка узла больше не выглядит нулём ----------
def test_json_rpc_ошибка_бросает_исключение(monkeypatch):
    """{"error": {...}} возвращался как словарь без result, и .get("result") давал None."""
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": 429, "message": "Too many requests"}}

    monkeypatch.setattr(helius.requests, "post", lambda *a, **k: R())
    with pytest.raises(RuntimeError, match="429"):
        helius.rpc("getTokenAccountsByOwner", [])


def test_успешный_ответ_проходит_как_прежде(monkeypatch):
    class R:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return {"result": {"value": []}}

    monkeypatch.setattr(helius.requests, "post", lambda *a, **k: R())
    assert helius.rpc("getSlot", [])["result"] == {"value": []}


def test_сбой_чтения_баланса_не_выдаётся_за_пустой_аккаунт(monkeypatch):
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a"})())
    monkeypatch.setattr(swap.helius, "rpc",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("RPC 429")))
    with pytest.raises(RuntimeError):
        swap.token_balance_raw("MINT")


# ---------- ожидание расчёта баланса ----------
def test_ожидание_возвращает_None_если_узлы_молчали(monkeypatch):
    """None и 0 — разные ответы: «не знаем» против «пусто»."""
    monkeypatch.setattr(swap, "token_balance_raw",
                        lambda m, url=None: (_ for _ in ()).throw(RuntimeError("узел молчит")))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_token_balance("MINT", 0.0, tries=3) is None


def test_ожидание_спрашивает_и_узел_отправки(monkeypatch):
    """Узел отправки транзакцию видел заведомо — он её и принял."""
    видели = []

    def _bal(m, url=None):
        видели.append(url)
        if url is None:
            raise RuntimeError("узел чтения отстаёт")
        return 1_000_000, 6, "ata"

    monkeypatch.setattr(swap, "token_balance_raw", _bal)
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_token_balance("MINT", 0.0, tries=2) == 1.0
    assert "https://send" in видели


def test_реальный_сдвиг_баланса_возвращается_сразу(monkeypatch):
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (1_500_000, 6, "ata"))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_token_balance("MINT", 1.0, tries=6) == 1.5


# ---------- подтверждённая покупка не теряется ----------
def _мокнуть_покупку(monkeypatch, settled):
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", True)
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a",
                                               "keypair": lambda s: "kp"})())
    monkeypatch.setattr(swap.market, "sol_price", lambda: 77.0)
    monkeypatch.setattr(swap, "_quote", lambda a, b, c: {"outAmount": "1000"})
    monkeypatch.setattr(swap, "_build_swap_tx", lambda q: {"swapTransaction": "x"})
    monkeypatch.setattr(swap, "_sign_and_send", lambda s: "SIG")
    monkeypatch.setattr(swap, "confirm", lambda s: True)
    monkeypatch.setattr(swap, "token_balance", lambda m: (0.0, None))
    monkeypatch.setattr(swap, "_settled_token_balance", lambda m, b, **k: settled)
    monkeypatch.setattr(swap.ledger, "record_intent", lambda *a, **k: "iid")
    monkeypatch.setattr(swap.ledger, "record_fill", lambda *a, **k: None)


def test_подтверждена_но_баланс_не_прочитан_позиция_ВСЁ_РАВНО_открывается(monkeypatch):
    """Ровно случай первой живой сделки: транзакция в блоке, узел молчит.

    Отказ здесь означал бы, что монитор откатит слот, а токены останутся
    в кошельке без присмотра. Выход продаёт ВЕСЬ остаток, поэтому точное
    количество для управления сделкой не нужно — теряем только замер.
    """
    _мокнуть_покупку(monkeypatch, None)
    r = swap.buy("MINT", 10.0)
    assert r["action"] == "bought"
    assert r["balance_unknown"] is True and r["tokens"] is None
    assert r["slippage_vs_quote"] is None


def test_подтверждена_и_баланс_не_вырос_это_по_прежнему_отказ(monkeypatch):
    """Узел ответил и сказал «ноль» — вот это настоящее расхождение, торговать вслепую нельзя."""
    _мокнуть_покупку(monkeypatch, 0.0)
    with pytest.raises(swap.SwapError, match="баланс не вырос"):
        swap.buy("MINT", 10.0)


# ---------- данные при работе не в полную силу ----------
def test_в_записи_выхода_есть_режим(monkeypatch, tmp_path):
    """Без mode живые и бумажные выходы в одном файле неразличимы навсегда."""
    written = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: written.append(o))
    monkeypatch.setattr(delivery, "send_telegram", lambda t: True)
    monkeypatch.setitem(strategy.EXECUTION, "LIVE_ENABLED", True)

    class P:
        token_mint = "T"
        entry_price = 1.0
        entry_ts = 1000.0
        entry_mc = 1000.0
        entry_actors = ["a"]
        exited_actors = ["a"]
        peak_price = 2.0
        remaining = 1.0
        realized = 0.0

    delivery.deliver_exit(P(), 1.5, "actors_exit", telegram=False)
    assert all(r["mode"] == "live" for r in written)
    written.clear()
    monkeypatch.setitem(strategy.EXECUTION, "LIVE_ENABLED", False)
    delivery.deliver_exit(P(), 1.5, "actors_exit", telegram=False)
    assert all(r["mode"] == "paper" for r in written)


def test_выручка_SOL_ждёт_расхождения_узлов(monkeypatch):
    """Первая живая продажа записала sol_actual = 0.0: узел чтения девять секунд
    отдавал баланс ДО сделки, код сдался и решил, что выручки не было."""
    seq = iter([2.0, 2.0, 2.0, 2.13])

    class W:
        def balance_sol(self, url=None):
            return next(seq, 2.13)

    monkeypatch.setattr(swap.wallet, "Wallet", lambda: W())
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_sol_balance(2.0, tries=6) == pytest.approx(2.13)


def test_закрытие_аккаунта_ждёт_обнуления(monkeypatch):
    """Закрытие идёт сразу после продажи, узел ещё отдаёт баланс ДО неё.

    Первая живая продажа из-за этого не вернула ренту 0.002039 SOL:
    аккаунт остался открытым с нулём внутри.
    """
    seq = iter([(10_004_341, 6, "ata"), (10_004_341, 6, "ata"), (0, 6, "ata")])
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: next(seq, (0, 6, "ata")))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a"})())
    monkeypatch.setitem(swap.strategy.EXECUTION, "LIVE_ENABLED", False)
    r = swap.close_token_account("MINT")
    assert r["action"] == "dry_run", "дождались нуля и дошли до закрытия"


def test_закрытие_не_идёт_вслепую_при_молчащих_узлах(monkeypatch):
    monkeypatch.setattr(swap, "token_balance_raw",
                        lambda m, url=None: (_ for _ in ()).throw(RuntimeError("429")))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    monkeypatch.setattr(swap.wallet, "Wallet",
                        lambda: type("W", (), {"available": True, "address": "a"})())
    r = swap.close_token_account("MINT")
    assert r["action"] == "skip" and "не прочитан" in r["reason"]


def test_продажи_акторов_пишутся_и_без_позиции(monkeypatch):
    """86% выходов — actor-exit. Для НЕВЗЯТЫХ сигналов это единственный след,
    по которому потом можно восстановить, чем сделка бы кончилась."""
    written = []
    monkeypatch.setattr(delivery, "_append", lambda p, o: written.append((p.name, o)))
    delivery.log_actor_sell_any("TOK", "actorA", 1.23, 5000.0)
    имя, rec = written[0]
    assert имя == "actor_sells_all.jsonl"
    assert rec == {"ts": 5000.0, "token_mint": "TOK", "actor": "actorA", "sell_price": 1.23}
