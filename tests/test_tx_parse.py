"""Тесты парсера сделок — от него зависит ЦЕНА ВХОДА (а значит весь учёт PnL)."""
import pytest

from src import tx_parse

W = "WalletAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
MINT = "TokenMintAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
WSOL = tx_parse.WSOL
USDC = tx_parse.USDC


def _tx(sol_delta_lamports, pre_tok, post_tok, fee=5000, pre_stable=0.0, post_stable=0.0, err=None):
    def bal(mint, owner, amt):
        return {"mint": mint, "owner": owner, "uiTokenAmount": {"uiAmount": amt}}
    pre, post = [], []
    if pre_tok is not None:
        pre.append(bal(MINT, W, pre_tok))
    if post_tok is not None:
        post.append(bal(MINT, W, post_tok))
    if pre_stable:
        pre.append(bal(USDC, W, pre_stable))
    if post_stable:
        post.append(bal(USDC, W, post_stable))
    return {"result": {
        "meta": {"err": err, "fee": fee,
                 "preBalances": [1_000_000_000], "postBalances": [1_000_000_000 + sol_delta_lamports],
                 "preTokenBalances": pre, "postTokenBalances": post},
        "transaction": {"message": {"accountKeys": [{"pubkey": W}]}},
        "blockTime": 1700000000}}


@pytest.fixture
def rpc(monkeypatch):
    def _set(payload):
        monkeypatch.setattr(tx_parse.helius, "rpc", lambda *a, **k: payload)
    return _set


def test_покупка_за_sol(rpc):
    rpc(_tx(-500_000_000, None, 1000.0))          # потратил 0.5 SOL, получил 1000 токенов
    t = tx_parse.parse_trade("sig", W)
    assert t["side"] == "buy" and t["token_mint"] == MINT
    assert t["base_amount"] == 1000.0 and t["sol"] == pytest.approx(0.5)
    assert t["fee"] == pytest.approx(5000 / 1e9)   # fee отдельно → точная цена входа


def test_продажа_за_sol(rpc):
    rpc(_tx(+300_000_000, 1000.0, 0.0))
    t = tx_parse.parse_trade("sig", W)
    assert t["side"] == "sell" and t["base_amount"] == 1000.0 and t["sol"] == pytest.approx(0.3)


def test_продажа_за_usdc_не_теряется(rpc):
    """Раньше терялась (sol_delta<0 из-за fee) → actor-exit молчал (аудит-2)."""
    rpc(_tx(-5000, 1000.0, 0.0, pre_stable=0.0, post_stable=250.0))
    t = tx_parse.parse_trade("sig", W)
    assert t is not None and t["side"] == "sell"
    assert t["usd_proceeds"] == pytest.approx(250.0) and t["sol"] == 0.0


def test_неудачная_транзакция_игнорируется(rpc):
    rpc(_tx(-500_000_000, None, 1000.0, err={"InstructionError": []}))
    assert tx_parse.parse_trade("sig", W) is None


def test_чужой_кошелёк_игнорируется(rpc):
    payload = _tx(-500_000_000, None, 1000.0)
    payload["result"]["transaction"]["message"]["accountKeys"] = [{"pubkey": "ДругойКошелёк"}]
    rpc(payload)
    assert tx_parse.parse_trade("sig", W) is None


def test_без_движения_токена_нет_сделки(rpc):
    rpc(_tx(-5000, None, None))
    assert tx_parse.parse_trade("sig", W) is None


def test_wsol_не_считается_торгуемым_токеном(rpc):
    """WSOL — котировка, не «купленный мем»: иначе врап SOL выглядел бы покупкой."""
    payload = _tx(-500_000_000, None, None)
    payload["result"]["meta"]["postTokenBalances"] = [
        {"mint": WSOL, "owner": W, "uiTokenAmount": {"uiAmount": 0.5}}]
    rpc(payload)
    assert tx_parse.parse_trade("sig", W) is None


def test_rpc_ошибка_не_роняет_парсер(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("сеть")
    monkeypatch.setattr(tx_parse.helius, "rpc", boom)
    assert tx_parse.parse_trade("sig", W) is None
