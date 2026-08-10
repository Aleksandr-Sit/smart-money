"""Тесты движка сигнала, конфига стратегии и разбора бондинг-кривой."""
import pytest
import yaml

from src import price_track, strategy
from src.signal_engine import BuyEvent, SignalEngine

AMAP = {"w1": ("actor1", 10.0), "w1b": ("actor1", 10.0), "w2": ("actor2", 8.0), "w3": ("actor3", 5.0)}


def _eng(**over):
    return SignalEngine(AMAP, {**over})


def test_конфлюенс_считает_акторов_а_не_кошельки():
    """Два кошелька одного актора (ротация) != конфлюенс."""
    e = _eng()
    assert e.process(BuyEvent(100, "T", "w1", usd=100)) is None
    assert e.process(BuyEvent(105, "T", "w1b", usd=100)) is None   # тот же актор
    assert e.process(BuyEvent(110, "T", "w2", usd=100)) is not None  # 2-й актор → сигнал


def test_strong_на_третьем_акторе_и_нет_дублей():
    e = _eng()
    e.process(BuyEvent(100, "T", "w1", usd=100))
    assert e.process(BuyEvent(110, "T", "w2", usd=100)).level == "weak"
    s = e.process(BuyEvent(120, "T", "w3", usd=100))
    assert s.level == "strong" and s.n_actors == 3
    assert e.process(BuyEvent(130, "T", "w1b", usd=100)) is None    # апгрейда нет → без дубля


def test_quiet_флаг_по_порогу_объёма():
    e = _eng()
    e.process(BuyEvent(100, "T", "w1", usd=50))
    s = e.process(BuyEvent(110, "T", "w2", usd=50))
    assert s.quiet is True and s.window_usd == 100
    e2 = _eng()
    e2.process(BuyEvent(100, "X", "w1", usd=400))
    assert e2.process(BuyEvent(110, "X", "w2", usd=400)).quiet is False


def test_strength_не_награждает_громкие():
    """Аудит-2: старая формула росла с объёмом = с убытком. Тихий должен быть >= громкого."""
    e1 = _eng()
    e1.process(BuyEvent(100, "A", "w1", usd=50))
    quiet = e1.process(BuyEvent(110, "A", "w2", usd=50))
    e2 = _eng()
    e2.process(BuyEvent(100, "B", "w1", usd=5000))
    loud = e2.process(BuyEvent(110, "B", "w2", usd=5000))
    assert quiet.strength >= loud.strength


def test_пыль_и_не_watchlist_игнорируются():
    e = _eng()
    assert e.process(BuyEvent(100, "T", "w1", usd=1)) is None        # ниже SIGNAL_MIN_USD
    assert e.process(BuyEvent(100, "T", "чужой", usd=500)) is None   # не в watchlist


def test_окно_конфлюенса_истекает():
    e = _eng()
    e.process(BuyEvent(100, "T", "w1", usd=100))
    assert e.process(BuyEvent(100 + 10_000, "T", "w2", usd=100)) is None   # далеко за окном


# ---------- конфиг стратегии ----------
def test_конфиг_загружается_и_версионирован():
    assert strategy.VERSION and isinstance(strategy.VERSION, str)
    # 07.08: тейки отключены по замеру (+999$ без них). Пустой список допустим,
    # но если список НЕ пуст — элементы обязаны быть парами (множитель, доля).
    assert isinstance(strategy.EXIT["PARTIAL_TAKES"], list)
    assert all(isinstance(x, tuple) and len(x) == 2 for x in strategy.EXIT["PARTIAL_TAKES"])


def test_валидация_ловит_битые_пороги(tmp_path):
    """Битый конфиг должен ронять процесс, а не тихо торговать неверными порогами."""
    cfg = yaml.safe_load(open(strategy.PATH, encoding="utf-8"))
    cfg["exit"]["SL_MULT"] = 2.0            # стоп выше тейка — бессмыслица
    p = tmp_path / "bad.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError):
        strategy.load(p)


def test_валидация_ловит_перепродажу_долей(tmp_path):
    cfg = yaml.safe_load(open(strategy.PATH, encoding="utf-8"))
    cfg["exit"]["PARTIAL_TAKES"] = [[2.0, 0.7], [4.0, 0.7]]   # суммарно 140% позиции
    p = tmp_path / "bad2.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError):
        strategy.load(p)


def test_валидация_ловит_медленный_тик(tmp_path):
    """На 90с гранулярности edge исчезал (аудит-3) — конфиг обязан это запрещать."""
    cfg = yaml.safe_load(open(strategy.PATH, encoding="utf-8"))
    cfg["tracking"]["TICK_S"] = 90
    p = tmp_path / "bad3.yaml"
    p.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    with pytest.raises(ValueError):
        strategy.load(p)


# ---------- бондинг-кривая ----------
def test_pda_детерминирован_и_валиден():
    mint = "FeSENori1vjgUYP63oPPeNJtjXeLpm9sht8UN2k4pump"
    pda = price_track.bonding_curve_pda(mint)
    assert pda == price_track.bonding_curve_pda(mint)          # детерминизм
    assert 32 <= len(pda) <= 44
    assert price_track.b58encode(price_track.b58decode(pda)) == pda   # base58 round-trip


def test_разбор_кривой_и_грэдуэйшн():
    import struct
    live = b"\x00" * 8 + struct.pack("<QQQQQ?", 1_073_000_000_000_000, 30_000_000_000, 0, 0, 0, False)
    c = price_track.parse_curve(live)
    assert c and c["complete"] is False and c["price_sol"] > 0
    done = b"\x00" * 8 + struct.pack("<QQQQQ?", 0, 0, 0, 0, 10**15, True)
    assert price_track.parse_curve(done) is None     # грэдуировал → фолбэк на DexScreener


def test_акторы_в_порядке_первой_покупки():
    """actors[0] обязан быть тем, кто зашёл ПЕРВЫМ.

    Аудит 09.08: при ротации watchlist нужно понимать, потеряем ли мы сигнал
    совсем, исключив актора, или просто войдём позже с другим составом. Порядок
    держится на хронологичности st["buys"] и на том, что dict сохраняет порядок
    вставки — оба свойства неявные, поэтому закреплены тестом.
    """
    from src.signal_engine import BuyEvent, SignalEngine
    amap = {f"w{i}": (f"actor{i}", 1.0) for i in range(4)}
    eng = SignalEngine(amap, {"CONFLUENCE_N": 2, "STRONG_CONFLUENCE_N": 3,
                              "CONFLUENCE_WINDOW_S": 600, "QUIET_MAX_USD": 250,
                              "SIGNAL_MIN_USD": 1})
    # заходят в порядке 2 → 0 → 1
    for i, w in enumerate(("w2", "w0", "w1")):
        sig = eng.process(BuyEvent(ts=1000.0 + i, token_mint="TOK", wallet=w, usd=50.0))
    assert sig is not None
    assert sig.actors[0] == "actor2", f"первым зашёл actor2, а в списке {sig.actors}"
    assert sig.actors[:3] == ["actor2", "actor0", "actor1"]


def test_повторная_покупка_не_меняет_порядок():
    """DCA того же актора не должна двигать его в конец списка."""
    from src.signal_engine import BuyEvent, SignalEngine
    amap = {"wA": ("actorA", 1.0), "wB": ("actorB", 1.0)}
    eng = SignalEngine(amap, {"CONFLUENCE_N": 2, "STRONG_CONFLUENCE_N": 3,
                              "CONFLUENCE_WINDOW_S": 600, "QUIET_MAX_USD": 250,
                              "SIGNAL_MIN_USD": 1})
    eng.process(BuyEvent(ts=1000.0, token_mint="TOK", wallet="wA", usd=50.0))
    eng.process(BuyEvent(ts=1001.0, token_mint="TOK", wallet="wA", usd=50.0))   # DCA
    sig = eng.process(BuyEvent(ts=1002.0, token_mint="TOK", wallet="wB", usd=50.0))
    assert sig is not None and sig.actors == ["actorA", "actorB"]
