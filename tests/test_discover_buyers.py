"""Обратный поиск ранних покупателей — тесты на дисциплину, а не на цифры.

Модуль опасен ровно тем, чем провалился Контур 1: кандидаты находятся на тех же
данных, по которым отбирались победители. Тесты закрепляют защиты — знаменатель,
порог в два победителя, отделение своих от новых и оговорка в тексте отчёта.
"""
import pytest

from src import discover_buyers as db


@pytest.fixture
def цепь(monkeypatch):
    """Подменяем узел: сеть в тестах не нужна, а пауза 0.25с × вызовы съела бы прогон."""
    def подставить(страницы: dict, транзакции: dict):
        def _rpc(метод, params):
            if метод == "getSignaturesForAddress":
                ключ = params[1].get("before")
                return {"result": страницы.get(ключ, [])}
            return {"result": {"meta": {"logMessages": транзакции.get(params[0], [])}}}
        monkeypatch.setattr(db, "_rpc", _rpc)
        monkeypatch.setattr(db.price_track, "bonding_curve_pda", lambda m: "PDA")
    return подставить


def _подписи(имена):
    return [{"signature": s, "err": None} for s in имена]


def test_берутся_САМЫЕ_РАННИЕ_а_не_свежие(цепь, monkeypatch):
    """getSignaturesForAddress отдаёт от новых к старым. Взять начало списка значило бы
    собрать тех, кто покупал НА ПИКЕ, — прямо противоположную выборку."""
    цепь({None: _подписи(["новая", "средняя", "старая"])},
         {"старая": ["ev-старая"], "средняя": ["ev-средняя"], "новая": ["ev-новая"]})
    monkeypatch.setattr(db.log_parse, "_events",
                        lambda логи: [{"is_buy": True, "mint": "M",
                                       "user": логи[0].replace("ev-", "")}])
    assert db.ранние_покупатели("M", сколько=2) == ["старая", "средняя"]


def test_упавшие_транзакции_пропускаются(цепь, monkeypatch):
    п = _подписи(["новая", "старая"])
    п[1]["err"] = {"InstructionError": 1}
    цепь({None: п}, {"новая": ["ev-новая"]})
    monkeypatch.setattr(db.log_parse, "_events",
                        lambda логи: [{"is_buy": True, "mint": "M", "user": "новая"}])
    assert db.ранние_покупатели("M") == ["новая"]


def test_продажи_не_считаются_покупками(цепь, monkeypatch):
    цепь({None: _подписи(["с1"])}, {"с1": ["x"]})
    monkeypatch.setattr(db.log_parse, "_events",
                        lambda логи: [{"is_buy": False, "mint": "M", "user": "продавец"}])
    assert db.ранние_покупатели("M") == []


def test_чужой_токен_в_той_же_транзакции_не_засчитывается(цепь, monkeypatch):
    """В одной транзакции бывает несколько свопов — учитываем только наш mint."""
    цепь({None: _подписи(["с1"])}, {"с1": ["x"]})
    monkeypatch.setattr(db.log_parse, "_events",
                        lambda логи: [{"is_buy": True, "mint": "ДРУГОЙ", "user": "W"},
                                      {"is_buy": True, "mint": "M", "user": "НАШ"}])
    assert db.ранние_покупатели("M") == ["НАШ"]


@pytest.fixture
def сбор(monkeypatch):
    def подставить(поб, покупатели, watchlist=()):
        monkeypatch.setattr(db, "победители", lambda мин_рост=10.0: поб)
        monkeypatch.setattr(db, "ранние_покупатели",
                            lambda m, сколько=25: покупатели.get(m, []))
        monkeypatch.setattr(db, "_свои_кошельки", lambda: set(watchlist))
    return подставить


def test_кошелёк_из_одного_победителя_не_кандидат(сбор):
    """Один удачный токен — это совпадение. Порог в два победителя existiert именно
    для того, чтобы случайный участник не выглядел находкой."""
    сбор([(20.0, "A"), (15.0, "B")], {"A": ["одиночка", "оба"], "B": ["оба"]})
    d = db.собрать()
    assert [k["кошелёк"] for k in d["кандидаты"]] == ["оба"]


def test_свои_отделены_от_новых(сбор):
    сбор([(20.0, "A"), (15.0, "B")], {"A": ["свой", "новый"], "B": ["свой", "новый"]},
         watchlist=["свой"])
    d = db.собрать()
    assert [k["кошелёк"] for k in d["новых"]] == ["новый"]
    assert {k["кошелёк"]: k["в_списке"] for k in d["кандидаты"]} == {"свой": True,
                                                                    "новый": False}


def test_считается_знаменатель(сбор):
    """Доля от РАЗОБРАННЫХ токенов, а не от заявленных: если половина не поднялась
    из цепи, кандидат с двумя попаданиями из двух — это 100%, и знать это важно."""
    сбор([(20.0, "A"), (15.0, "B")], {"A": ["W", "W2"], "B": ["W", "W2"]})
    d = db.собрать()
    assert d["разобрано"] == 2
    assert d["кандидаты"][0]["доля"] == pytest.approx(1.0)


def test_токен_без_цепи_не_попадает_в_знаменатель(сбор):
    сбор([(20.0, "A"), (15.0, "B"), (12.0, "C")], {"A": ["W", "W2"], "B": ["W", "W2"]})
    assert db.собрать()["разобрано"] == 2


def test_отчёт_несёт_оговорку_про_ошибку_выжившего(сбор):
    сбор([(20.0, "A"), (15.0, "B")], {"A": ["W", "W2"], "B": ["W", "W2"]})
    т = db.отчёт(db.собрать())
    assert "ошибка выжившего" in т and "испытательный срок" in т
    assert "НЕ РЕЙТИНГ" in т


def test_пустой_результат_говорит_прямо(сбор):
    сбор([(20.0, "A")], {"A": ["W"]})
    assert "новых кошельков не найдено" in db.отчёт(db.собрать())


def test_watchlist_читается_как_единый_json(tmp_path, monkeypatch):
    """Первый прогон читал flow_watchlist.json построчно через read_jsonl — а это
    один JSON-документ. Множество «своих» выходило пустым, и отчёт объявил новыми
    ВСЕХ кандидатов, включая CkLT4ADy, который в списке с самого начала."""
    import json
    monkeypatch.setattr(db.config, "OUTPUT_DIR", tmp_path)
    (tmp_path / "flow_watchlist.json").write_text(
        json.dumps([{"actor_id": "A1", "wallet": "КОШЕЛЁК1"}, {"wallet": "КОШЕЛЁК2"}]),
        encoding="utf-8")
    свои = db._свои_кошельки()
    assert "КОШЕЛЁК1" in свои and "КОШЕЛЁК2" in свои


def test_watchlist_словарём_тоже_читается(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(db.config, "OUTPUT_DIR", tmp_path)
    (tmp_path / "flow_watchlist.json").write_text(
        json.dumps({"W1": {"wallet": "W1"}, "W2": {"wallet": "W2"}}), encoding="utf-8")
    assert db._свои_кошельки() == {"W1", "W2"}


def test_нет_файла_watchlist_не_роняет_разбор(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "OUTPUT_DIR", tmp_path)
    assert db._свои_кошельки() == set()
