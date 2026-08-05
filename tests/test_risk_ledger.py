"""Тесты защитного контура (Фаза C). Ошибка здесь = потеря депозита, а не просто баг."""
import json

import pytest

from src import ledger, risk


@pytest.fixture
def rm(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "OUTPUT_DIR", tmp_path)
    return risk.RiskManager(path=tmp_path / "risk_state.json")


def _blow_daily_stop(m, loss=-0.5):
    """Пробить дневной стоп сделками по `loss`. Считаем от КОНФИГА, не от констант —
    иначе тесты ломаются при каждой перекалибровке (05.08: стоп 10%→25%, клип $20→$10)."""
    need = int(m.daily_stop * m.bankroll / (abs(loss) * m.clip)) + 2
    for _ in range(need):
        t = m.on_close(loss)
        if t:
            return t
    return None


# ---------- дневной стоп ----------
def test_дневной_стоп_срабатывает_и_блокирует_входы(rm):
    assert rm.gate(0)[0] is True
    tripped = _blow_daily_stop(rm)
    assert tripped is not None                       # стоп = DAILY_STOP_FRAC × банк
    assert rm.state.realized_usd <= -0.10 * rm.bankroll
    rm.mode = "enforce"                      # в live денежные лимиты блокируют
    allowed, reason, blocked = rm.gate(0)
    assert allowed is False and "остановлена" in reason and blocked is True


def test_прибыль_не_вызывает_стоп(rm):
    for _ in range(30):
        assert rm.on_close(+0.5) is None
    assert rm.gate(0)[0] is True


def test_стоп_переживает_рестарт(rm, tmp_path, monkeypatch):
    """КРИТИЧНО: перезапуск не должен возобновлять торговлю после лимита потерь."""
    _blow_daily_stop(rm)
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
    _blow_daily_stop(rm)
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
    rm.bankroll = rm.clip * 1.5               # банк меньше 2 клипов (от конфига)
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


def test_каждая_нога_сделки_имеет_своё_намерение(tmp_path, monkeypatch):
    """РЕГРЕСС 05.08: продажа привязывалась к намерению ПОКУПКИ → у 662 из 679 намерений
    было по 2 исполнения, а 'median_slippage' считал PnL сделки вместо проскальзывания.
    В live это ослепило бы нас именно к качеству исполнения."""
    monkeypatch.setattr(ledger.config, "OUTPUT_DIR", tmp_path)
    bid = ledger.record_intent("buy", "TOK", 1e-5)
    ledger.record_fill(bid, "TOK", 1e-5, usd=20, extra={"position_id": bid})
    sid = ledger.record_intent("sell", "TOK", 2e-5, extra={"position_id": bid})   # СВОЁ намерение
    ledger.record_fill(sid, "TOK", 2e-5, usd=40, extra={"position_id": bid})

    rows = ledger.load()
    intents = [r for r in rows if r["type"] == "intent"]
    fills = [r for r in rows if r["type"] == "fill"]
    assert len(intents) == len(fills) == 2          # 1:1, не 1:2
    assert sid != bid
    from collections import Counter
    assert max(Counter(f["intent_id"] for f in fills).values()) == 1

    r = ledger.reconcile()
    assert r["unfilled"] == 0 and r["ok"] is True
    # slippage теперь честный (исполнение по цене намерения), а НЕ +100% PnL сделки
    assert r["median_slippage"] == pytest.approx(0.0, abs=1e-9)
    # обе ноги связаны position_id
    assert {f.get("position_id") for f in fills} == {bid}
