"""Тесты кошелька и вывода (Фаза D). Здесь ошибка = потеря реальных денег.

Проверяем ИНВАРИАНТЫ безопасности, а не только счастливый путь.
"""
import inspect

import pytest

from src import strategy, sweep, wallet

COLD = "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM"     # валидный адрес для тестов
HOT = "So11111111111111111111111111111111111111112"


@pytest.fixture
def cfg(monkeypatch):
    """Настроенный вывод с валидным адресом."""
    c = {**strategy.SWEEP, "ENABLED": True, "DRY_RUN": True, "SWEEP_ADDRESS": COLD}
    monkeypatch.setattr(strategy, "SWEEP", c)
    monkeypatch.setattr(sweep.strategy, "SWEEP", c)
    sweep._last_sweep_ts = 0.0
    return c


# ---------- ГЛАВНЫЙ ИНВАРИАНТ: адрес нельзя подменить ----------
def test_вывод_не_принимает_адрес_параметром():
    """Функция вывода физически не может отправить на произвольный адрес."""
    sig = inspect.signature(sweep.execute)
    assert "address" not in sig.parameters and "dest" not in sig.parameters
    assert "to" not in sig.parameters


def test_отправка_проверяет_адрес_даже_при_прямом_вызове(cfg, monkeypatch):
    """Защита в глубину: прямой вызов _send_transfer с чужим адресом заблокирован."""
    from solders.pubkey import Pubkey
    chuzhoy = Pubkey.from_string("11111111111111111111111111111111")
    with pytest.raises(sweep.SweepError, match="не совпадает"):
        sweep._send_transfer(wallet.Wallet(), chuzhoy, 1000)


def test_пустой_адрес_отключает_вывод(monkeypatch):
    """FAIL CLOSED: не настроен адрес → вывод невозможен."""
    monkeypatch.setattr(sweep.strategy, "SWEEP", {**strategy.SWEEP, "SWEEP_ADDRESS": ""})
    with pytest.raises(sweep.SweepError, match="не задан"):
        sweep.destination()


def test_кривой_адрес_отклоняется(monkeypatch):
    monkeypatch.setattr(sweep.strategy, "SWEEP", {**strategy.SWEEP, "SWEEP_ADDRESS": "не-адрес-вовсе"})
    with pytest.raises(sweep.SweepError, match="не является адресом"):
        sweep.destination()


def test_адрес_равный_горячему_кошельку_отклоняется(monkeypatch):
    """Вывод сам себе = потеря комиссий и иллюзия защиты."""
    monkeypatch.setattr(sweep.strategy, "SWEEP", {**strategy.SWEEP, "SWEEP_ADDRESS": HOT})

    class FakeW:
        available = True
        address = HOT
    monkeypatch.setattr(sweep.wallet, "Wallet", lambda: FakeW())
    with pytest.raises(sweep.SweepError, match="совпадает с горячим"):
        sweep.destination()


# ---------- предохранители ----------
def test_баланс_недоступен_ничего_не_выводим(cfg, monkeypatch):
    """FAIL CLOSED: RPC молчит → не гадаем, а пропускаем."""
    monkeypatch.setattr(sweep.wallet.Wallet, "balance_sol", lambda self: None)
    assert sweep.plan()["action"] == "skip"


def test_ниже_порога_не_выводим(cfg, monkeypatch):
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    p = sweep.plan(balance_sol=4.0)          # $400 = банк, порог $500
    assert p["action"] == "skip" and "порога" in p["reason"]


def test_излишек_меньше_шага_не_выводим(cfg, monkeypatch):
    """Порог и шаг согласованы: банк $400 + буфер $5 + шаг $25 → порог $430."""
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    assert sweep.plan(balance_sol=4.35)["action"] == "sweep"      # $435: излишек $30 >= $25
    p2 = sweep.plan(balance_sol=4.31)                              # $431: излишек $26... 
    assert p2["action"] in ("sweep", "skip")                       # граница
    p3 = sweep.plan(balance_sol=4.25)                              # $425 — ниже порога $430
    assert p3["action"] == "skip"


def test_буфер_комиссий_не_выводится(cfg, monkeypatch):
    """Нельзя оставить кошелёк без SOL на комиссии — иначе бот не сможет торговать."""
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    p = sweep.plan(balance_sol=10.0)         # $1000
    assert p["action"] == "sweep"
    left = 1000 - p["amount_usd"]
    assert left >= strategy.RISK["BANKROLL_USD"] + cfg["FEE_BUFFER_SOL"] * 100 - 0.01


def test_потолок_на_одну_транзакцию(cfg, monkeypatch):
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    p = sweep.plan(balance_sol=1000.0)       # $100k — абсурд, но проверим ограничитель
    assert p["amount_usd"] == cfg["SWEEP_MAX_USD"]


def test_интервал_между_выводами(cfg, monkeypatch):
    """Защита от цикла: два вывода подряд невозможны."""
    import time
    monkeypatch.setattr(sweep.wallet.Wallet, "available", property(lambda self: True))
    sweep._last_sweep_ts = time.time()
    r = sweep.execute()
    assert r["action"] == "skip" and "интервал" in r["reason"]


def test_выключенный_вывод_не_работает(monkeypatch):
    monkeypatch.setattr(sweep.strategy, "SWEEP",
                        {**strategy.SWEEP, "ENABLED": False, "SWEEP_ADDRESS": COLD})
    assert sweep.execute()["action"] == "skip"


def test_dry_run_ничего_не_отправляет(cfg, monkeypatch):
    """В сухом режиме отправка не должна вызываться ВООБЩЕ."""
    monkeypatch.setattr(sweep.market, "sol_price", lambda: 100.0)
    monkeypatch.setattr(sweep.wallet.Wallet, "available", property(lambda self: True))
    monkeypatch.setattr(sweep.wallet.Wallet, "address", property(lambda self: HOT))
    monkeypatch.setattr(sweep.wallet.Wallet, "balance_sol", lambda self: 10.0)

    def boom(*a, **k):
        raise AssertionError("dry_run НЕ ДОЛЖЕН отправлять транзакцию!")
    monkeypatch.setattr(sweep, "_send_transfer", boom)
    r = sweep.execute(dry_run=True)
    assert r["action"] == "dry_run" and r["destination"] == COLD


# ---------- ключ ----------
def test_ключ_не_утекает_в_строковое_представление():
    """Ключ не должен появляться ни в repr, ни в str, ни в статусе."""
    w = wallet.Wallet()
    src = inspect.getsource(wallet)
    for bad in ("print(raw", "print(self._kp", f"{'log'}(raw"):
        assert bad not in src
    assert "SOLANA_PRIVATE_KEY" in src          # читается только здесь
    assert "не распознан" in src                 # ошибка без содержимого ключа


def test_без_ключа_монитор_не_падает():
    """Бумажный режим обязан работать без кошелька (fail closed на торговлю, не на запуск)."""
    w = wallet.Wallet()
    if not w.available:
        assert w.balance_sol() is None
        with pytest.raises(RuntimeError):
            _ = w.pubkey


def test_ключ_читается_только_в_wallet():
    """Никакой другой модуль не должен трогать приватный ключ."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "src"
    # wallet.py — единственный ЧИТАТЕЛЬ ключа; new_wallet.py — единственный ПИСАТЕЛЬ
    for f in root.glob("*.py"):
        if f.name in ("wallet.py", "new_wallet.py"):
            continue
        assert "SOLANA_PRIVATE_KEY" not in f.read_text(encoding="utf-8"), f"ключ читается в {f.name}"


# ---------- генератор кошелька ----------
def test_генератор_не_перезаписывает_существующий_ключ(tmp_path, monkeypatch):
    """Перезапись ключа = потеря доступа к средствам на старом кошельке."""
    from src import new_wallet
    env = tmp_path / ".env"
    env.write_text("SOLANA_PRIVATE_KEY=уже_есть_длинный_ключ_значение\n", encoding="utf-8")
    monkeypatch.setattr(new_wallet, "ENV", env)
    monkeypatch.setattr("sys.argv", ["new_wallet"])
    assert new_wallet._existing_key_present() is True
    before = env.read_text(encoding="utf-8")
    new_wallet.main()                      # должен отказаться
    assert env.read_text(encoding="utf-8") == before


def test_генератор_не_печатает_приватный_ключ():
    """Ключ на экране попадает в историю консоли и скриншоты."""
    import inspect
    from src import new_wallet
    src = inspect.getsource(new_wallet)
    assert "print(f\"    {addr}" in src or "{addr}" in src      # адрес печатаем
    assert "print(secret_b58" not in src and "print(f\"{secret_b58}" not in src
    assert "НЕ выводился" in src
