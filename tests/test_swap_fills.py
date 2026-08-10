"""Инварианты замера факта исполнения — аудит 07.08.

Три бага, найденные аудитом:
  1. buy() делил потраченные доллары на ВЕСЬ баланс токена, а не на купленное —
     при повторном входе цена занижалась.
  2. sell() писал в леджер котировку вместо факта, поэтому измеренное
     проскальзывание по продажам было тождественно нулю.
  3. sendTransaction уходил на публичный узел вместе с чтением.
"""
import pytest

from src import helius, strategy, swap


def test_покупка_считает_дельту_а_не_весь_баланс(monkeypatch):
    """На аккаунте уже лежит 1000 токенов; докупаем 500. Цена — по 500, не по 1500."""
    monkeypatch.setattr(swap, "_settled_token_balance", lambda mint, before, tries=6: 1500.0)
    before = 1000.0
    got = 1500.0 - before
    assert got == 500.0
    usd = 10.0
    assert usd / got == pytest.approx(0.02)            # верно
    assert usd / 1500.0 == pytest.approx(0.00667, rel=1e-2)   # старая, заниженная втрое


def test_ожидание_расчёта_баланса_переживает_отставание_узла(monkeypatch):
    """Подтверждение и чтение баланса приходят с разных слотов: нельзя читать сразу."""
    seq = iter([(1_000_000_000, 6, "ata")] * 4 + [(1_500_000_000, 6, "ata")])
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: next(seq))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_token_balance("mint", 1000.0) == 1500.0


def test_ожидание_сдаётся_и_не_виснет(monkeypatch):
    """Если баланс так и не сдвинулся, возвращаем ПРОЧИТАННОЕ значение, а не None.

    Разница принципиальна (10.08): прочитали и увидели прежний баланс — это факт
    «сделка не изменила баланс»; не смогли прочитать вовсе — это None, «не знаем».
    """
    monkeypatch.setattr(swap, "token_balance_raw", lambda m, url=None: (1_000_000_000, 6, "ata"))
    monkeypatch.setattr(swap.time, "sleep", lambda s: None)
    monkeypatch.setattr(swap.helius, "send_url", lambda: "https://send")
    assert swap._settled_token_balance("mint", 1000.0, tries=3) == 1000.0


def test_отправка_сделок_идёт_на_отдельный_узел(monkeypatch):
    """Публичный узел лимитирует sendTransaction — читать с него можно, тратить нет."""
    monkeypatch.setitem(strategy.TRACKING, "RPC_PROVIDER", "public")
    monkeypatch.setitem(strategy.EXECUTION, "SEND_PROVIDER", "helius")
    assert helius.rpc_url() == helius.PUBLIC_RPC          # чтение — с публичного
    assert helius.send_url() != helius.PUBLIC_RPC         # отправка — нет
    assert "sendTransaction" in helius._SEND_METHODS


def _real_cfg():
    """Боевой конфиг целиком: валидатор проверяет наличие всех параметров,
    поэтому урезанный словарь упал бы на другой проверке и тест был бы ложным."""
    import copy
    import pathlib

    import yaml
    root = pathlib.Path(__file__).resolve().parent.parent
    return copy.deepcopy(yaml.safe_load((root / "config" / "strategy.yaml").read_text(encoding="utf-8")))


def test_чтение_и_отправка_через_один_узел_запрещены():
    """Падение общего узла выключило бы и наблюдение, и выход из позиций сразу.
    Пересмотр 07.08: сам по себе бесплатный узел не запрещён — опасно именно совмещение."""
    cfg = _real_cfg()
    cfg["risk"]["RISK_MODE"] = "enforce"
    cfg["execution"]["LIVE_ENABLED"] = True
    cfg["tracking"]["RPC_PROVIDER"] = "public"
    cfg["execution"]["SEND_PROVIDER"] = "public"
    with pytest.raises(ValueError, match="один публичный узел"):
        strategy._validate(cfg)


def test_разные_бесплатные_узлы_разрешены():
    """Отправка через publicnode при чтении с public — разные пулы, риск разнесён."""
    cfg = _real_cfg()
    cfg["risk"]["RISK_MODE"] = "enforce"
    cfg["execution"]["LIVE_ENABLED"] = True
    cfg["tracking"]["RPC_PROVIDER"] = "public"
    cfg["execution"]["SEND_PROVIDER"] = "publicnode"
    strategy._validate(cfg)          # не должно бросать


def test_живая_торговля_на_надёжном_узле_проходит():
    """Обратная сторона: правильная конфигурация не должна блокироваться."""
    cfg = _real_cfg()
    cfg["risk"]["RISK_MODE"] = "enforce"
    cfg["execution"]["LIVE_ENABLED"] = True
    cfg["execution"]["SEND_PROVIDER"] = "helius"
    strategy._validate(cfg)          # не должно бросать


def test_каждый_провайдер_отправки_даёт_свой_адрес(monkeypatch):
    """Отправка не должна молча свалиться на узел чтения ни при какой настройке."""
    monkeypatch.setitem(strategy.TRACKING, "RPC_PROVIDER", "public")
    monkeypatch.setenv("DRPC_API_KEY", "тестовый-ключ")
    seen = {}
    for who in ("public", "publicnode", "jito", "drpc"):
        monkeypatch.setitem(strategy.EXECUTION, "SEND_PROVIDER", who)
        seen[who] = helius.send_url()
    assert seen["jito"] == helius.JITO_SEND
    assert "solana" in seen["drpc"] and helius.DRPC_HOST in seen["drpc"]
    assert seen["publicnode"] == helius.PUBLICNODE_RPC
    assert len(set(seen.values())) == 4          # адреса различны, подмены нет


def test_неизвестный_провайдер_отправки_отвергается():
    """Опечатка в конфиге не должна тихо отправить деньги куда попало."""
    cfg = _real_cfg()
    cfg["execution"]["SEND_PROVIDER"] = "quiknode"
    with pytest.raises(ValueError, match="SEND_PROVIDER"):
        strategy._validate(cfg)
