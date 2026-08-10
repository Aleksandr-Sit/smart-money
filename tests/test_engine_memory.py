"""Уборка состояния движка сигнала — и доказательство, что решения от неё не меняются.

Аудит 10.08: self.tokens рос без ограничений. Запись заводилась на каждый увиденный
токен и не удалялась никогда; окно покупок подрезалось только когда по этому токену
приходило новое событие, поэтому мёртвый токен уносил свой список покупок навсегда.
Замер по памяти боевого контейнера: 272.6 -> 273.2 МиБ за 24 минуты (~36 МБ/сутки).

Главное требование к правке: она про ПАМЯТЬ, а не про стратегию. Ни один сигнал не
должен появиться или исчезнуть из-за уборки.
"""
from src.signal_engine import BuyEvent, SignalEngine

AMAP = {"w1": ("A", 1.0), "w2": ("B", 1.0), "w3": ("C", 1.0)}
CFG = {"CONFLUENCE_N": 2, "STRONG_CONFLUENCE_N": 3, "CONFLUENCE_WINDOW_S": 600,
       "SIGNAL_MIN_USD": 20, "QUIET_MAX_USD": 250}


def _eng():
    return SignalEngine(AMAP, CFG)


def test_истёкшие_покупки_освобождаются():
    e = _eng()
    e.process(BuyEvent(1000, "TOK", "w1", usd=100))
    assert len(e.tokens["TOK"]["buys"]) == 1
    e._prune(now=1000 + 601)                      # окно истекло
    assert len(e.tokens["TOK"]["buys"]) == 0


def test_свежие_покупки_уборка_не_трогает():
    e = _eng()
    e.process(BuyEvent(1000, "TOK", "w1", usd=100))
    e._prune(now=1000 + 599)                      # окно ещё не истекло
    assert len(e.tokens["TOK"]["buys"]) == 1


def test_уборка_сохраняет_last_n_и_не_плодит_повторных_сигналов():
    """Ключевое свойство: сброс last_n был бы ИЗМЕНЕНИЕМ ПРАВИЛА ВХОДА, а не уборкой.

    Без сохранения last_n тот же токен, набрав двух акторов заново через час,
    выдал бы новый сигнал — то есть поток сделок вырос бы от правки про память.
    """
    e = _eng()
    assert e.process(BuyEvent(1000, "TOK", "w1", usd=100)) is None
    assert e.process(BuyEvent(1010, "TOK", "w2", usd=100)) is not None   # N=2, сигнал
    e._prune(now=1010 + 601)
    assert e.tokens["TOK"]["last_n"] == 2, "уровень конфлюенса обязан пережить уборку"
    # два актора заново, час спустя — сигнала быть НЕ должно (уровень тот же)
    assert e.process(BuyEvent(5000, "TOK", "w1", usd=100)) is None
    assert e.process(BuyEvent(5010, "TOK", "w2", usd=100)) is None


def test_апгрейд_до_трёх_акторов_после_уборки_работает():
    """Обратная проверка: уборка не должна ГЛУШИТЬ законный сигнал более высокого уровня."""
    e = _eng()
    e.process(BuyEvent(1000, "TOK", "w1", usd=100))
    e.process(BuyEvent(1010, "TOK", "w2", usd=100))     # N=2
    e._prune(now=1700)
    for ts, w in ((5000, "w1"), (5010, "w2"), (5020, "w3")):
        sig = e.process(BuyEvent(ts, "TOK", w, usd=100))
    assert sig is not None and sig.n_actors == 3


def test_потолок_числа_токенов_вытесняет_самые_старые(monkeypatch):
    import src.signal_engine as se
    monkeypatch.setattr(se, "MAX_TOKENS", 10)
    e = _eng()
    for i in range(25):
        e.process(BuyEvent(1000 + i, f"TOK{i}", "w1", usd=100))
    e._prune(now=1000)
    assert len(e.tokens) == 10
    assert "TOK0" not in e.tokens and "TOK24" in e.tokens, "вытесняются именно старые"


def test_уборка_вызывается_сама_по_счётчику(monkeypatch):
    import src.signal_engine as se
    monkeypatch.setattr(se, "PRUNE_EVERY", 5)
    e = _eng()
    calls = []
    monkeypatch.setattr(e, "_prune", lambda now: calls.append(now))
    for i in range(12):
        e.process(BuyEvent(1000 + i, f"T{i}", "w1", usd=100))
    assert len(calls) == 2, "раз в PRUNE_EVERY событий, не чаще и не реже"


def test_поток_сигналов_идентичен_с_уборкой_и_без(monkeypatch):
    """Сквозная проверка на одном и том же потоке событий: множества сигналов совпадают."""
    import src.signal_engine as se
    события = []
    for t in range(0, 4000, 7):
        события.append(BuyEvent(1000 + t, f"TOK{t % 40}", ["w1", "w2", "w3"][t % 3], usd=100))

    def прогон():
        eng = _eng()                    # ОДИН движок на весь поток, иначе тест ничего не мерит
        out = []
        for e in события:
            s = eng.process(e)
            if s:
                out.append((s.token_mint, s.ts, s.n_actors))
        return out

    monkeypatch.setattr(se, "PRUNE_EVERY", 10 ** 9)      # уборка практически отключена
    без = прогон()
    monkeypatch.setattr(se, "PRUNE_EVERY", 3)            # уборка на каждом третьем
    с_уборкой = прогон()

    assert без == с_уборкой, "уборка памяти не имеет права менять решения движка"
    assert без, "тест бессмысленен, если сигналов не было вовсе"
