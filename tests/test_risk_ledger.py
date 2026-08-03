"""Тесты защитного контура (Фаза C). Ошибка здесь = потеря депозита, а не просто баг."""
import json

import pytest

from src import ledger, risk


@pytest.fixture
def rm(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "OUTPUT_DIR", tmp_path)
    return risk.RiskManager(path=tmp_path / "risk_state.json")


# ---------- дневной стоп ----------
def test_дневной_стоп_срабатывает_и_блокирует_входы(rm):
    assert rm.gate(0)[0] is True
    tripped = None
    for _ in range(10):
        tripped = rm.on_close(-0.5)          # -50% × клип $20 = -$10 за сделку
        if tripped:
            break
    assert tripped is not None                       # стоп на -$50 (10% от $500)
    assert rm.state.realized_usd <= -0.10 * rm.bankroll
    rm.mode = "enforce"                      # в live денежные лимиты блокируют
    allowed, reason, blocked = rm.gate(0)
    assert allowed is False and "остановлена" in reason and blocked is True


def test_прибыль_не_вызывает_стоп(rm):
    for _ in range(20):
        assert rm.on_close(+0.5) is None
    assert rm.gate(0)[0] is True


def test_стоп_переживает_рестарт(rm, tmp_path, monkeypatch):
    """КРИТИЧНО: перезапуск не должен возобновлять торговлю после лимита потерь."""
    for _ in range(10):
        if rm.on_close(-0.5):
            break
    monkeypatch.setattr(risk.config, "OUTPUT_DIR", tmp_path)
    fresh = risk.RiskManager(path=tmp_path / "risk_state.json")
    assert fresh.state.halted is True
    fresh.mode = "enforce"
    assert fresh.gate(0)[0] is False


def test_битое_состояние_запрещает_торговлю(tmp_path, monkeypatch):
    """FAIL CLOSED: не смогли прочитать состояние → не торгуем."""
    monkeypatch.setattr(risk.config, "OUTPUT_DIR", tmp_path)
    p = tmp_path / "risk_state.json"
    p.write_text("{битый json", encoding="utf-8")
    m = risk.RiskManager(path=p)
    m.mode = "enforce"
    assert m.state.halted is True and m.gate(0)[0] is False


def test_новые_сутки_снимают_дневной_стоп(rm):
    for _ in range(10):
        if rm.on_close(-0.5):
            break
    assert rm.state.halted is True
    rm.state.day = "2020-01-01"              # имитируем наступление новых UTC-суток
    rm._roll_day()
    assert rm.state.halted is False and rm.state.realized_usd == 0.0


def test_серия_убытков_не_срабатывает_на_обычной_серии(rm):
    """У стратегии ~57% убыточных: 10 минусов подряд — норма, останавливать нельзя."""
    rm.daily_stop = 10.0                     # отключаем дневной стоп, проверяем только серию
    for _ in range(10):
        assert rm.on_close(-0.01) is None
    assert rm.state.loss_streak == 10


# ---------- kill-switch ----------
def test_kill_switch_блокирует_даже_в_shadow(rm, tmp_path):
    """Аварийный тормоз обязан работать в ЛЮБОМ режиме — это ручное вмешательство."""
    rm.mode = "shadow"
    assert rm.gate(0)[0] is True
    (tmp_path / "KILL").write_text("", encoding="utf-8")
    allowed, reason, _ = rm.gate(0)
    assert allowed is False and "KILL" in reason
    (tmp_path / "KILL").unlink()
    assert rm.gate(0)[0] is True


# ---------- экспозиция ----------
def test_лимит_позиций_жёсткий_в_любом_режиме(rm):
    rm.mode = "shadow"
    assert rm.gate(rm.max_positions)[0] is False      # часть валидированной стратегии


def test_экспозиция_мягкая_в_shadow_жёсткая_в_enforce(rm):
    rm.bankroll = 30                          # банк меньше 2 клипов
    rm.mode = "shadow"
    allowed, reason, blocked = rm.gate(1)
    assert allowed is True and blocked is True and "экспозиция" in reason   # копим данные
    rm.mode = "enforce"
    assert rm.gate(1)[0] is False                                          # деньги защищены


# ---------- леджер ----------
def test_леджер_сверка_намерение_исполнение(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.config, "OUTPUT_DIR", tmp_path)
    iid = ledger.record_intent("buy", "TOK", 1e-5, clip_usd=20)
    ledger.record_fill(iid, "TOK", 1.1e-5, usd=20)     # исполнение на 10% хуже намерения
    r = ledger.reconcile()
    assert r["intents"] == 1 and r["fills"] == 1 and r["unfilled"] == 0
    assert r["median_slippage"] == pytest.approx(0.1, abs=1e-6)
    assert r["ok"] is True


def test_леджер_ловит_исполнение_без_намерения(tmp_path, monkeypatch):
    """Тревога: в кошельке сделка, которую бот не планировал (взлом/двойная отправка)."""
    monkeypatch.setattr(ledger.config, "OUTPUT_DIR", tmp_path)
    ledger.record_fill("несуществующий", "TOK", 1e-5, usd=20)
    r = ledger.reconcile()
    assert r["orphan_fills"] == 1 and r["ok"] is False


def test_леджер_ловит_намерение_без_исполнения(tmp_path, monkeypatch):
    """В live это провалившаяся транзакция — деньги списаны, позиции нет."""
    monkeypatch.setattr(ledger.config, "OUTPUT_DIR", tmp_path)
    ledger.record_intent("buy", "TOK", 1e-5)
    assert ledger.reconcile()["unfilled"] == 1


def test_леджер_пишет_версию_стратегии(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.config, "OUTPUT_DIR", tmp_path)
    ledger.record_intent("buy", "TOK", 1e-5)
    rec = json.loads((tmp_path / ledger.PATH).read_text(encoding="utf-8").splitlines()[0])
    assert rec["strategy_version"] and rec["id"]
