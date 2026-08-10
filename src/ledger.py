"""Фаза C — леджер: независимый аудит-след сделок в ДОЛЛАРАХ + сверка.

Зачем отдельно от paper_closed.jsonl: тот пишет доли (realized_pnl), считается той же
логикой, что принимает решения. Леджер ведёт учёт НЕЗАВИСИМО и в деньгах — так расхождение
между «что стратегия думает» и «что реально в кошельке» становится видимым, а не скрытым.

Формат: append-only JSONL, у каждой записи свой id. Форма одинакова для paper/shadow/live —
переход к реальным деньгам будет сменой поля mode, а не переписыванием учёта.

  intent  — решение войти/выйти (что бот СОБИРАЕТСЯ сделать, по какой цене, каким клипом)
  fill    — фактическое исполнение (в live: подпись tx и реально полученное количество)
  Сверка: reconcile() сопоставляет intent↔fill и считает расхождение.
"""
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone

from . import config, strategy

PATH = "ledger.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(rec: dict) -> None:
    with open(config.OUTPUT_DIR / PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def record_intent(side: str, token: str, price: float | None, clip_usd: float | None = None,
                  frac: float = 1.0, reason: str = "", mode: str = "paper",
                  extra: dict | None = None) -> str:
    """Записать НАМЕРЕНИЕ (вход/выход). → intent_id для последующей привязки исполнения."""
    iid = uuid.uuid4().hex[:12]
    clip = clip_usd if clip_usd is not None else strategy.RISK["CLIP_USD"]
    _append({"ts": _now(), "type": "intent", "id": iid, "mode": mode, "side": side,
             "token_mint": token, "price": price, "clip_usd": clip, "frac": frac,
             "reason": reason, "strategy_version": strategy.VERSION, **(extra or {})})
    return iid


def record_fill(intent_id: str, token: str, price: float | None, usd: float,
                tokens: float | None = None, signature: str | None = None,
                mode: str = "paper", extra: dict | None = None) -> None:
    """Записать ИСПОЛНЕНИЕ. В paper/shadow — модельное; в live — фактическое (с подписью tx)."""
    _append({"ts": _now(), "type": "fill", "intent_id": intent_id, "mode": mode,
             "token_mint": token, "price": price, "usd": usd, "tokens": tokens,
             "signature": signature, **(extra or {})})


def record_reject(intent_id: str, token: str, reason: str, signature: str | None = None,
                  mode: str = "live", extra: dict | None = None) -> None:
    """Записать НЕСОСТОЯВШУЮСЯ сделку. Это НЕ исполнение — денег не двигалось.

    ЗАЧЕМ (найдено 10.08 при пересчёте отказов). Транзакция, долетевшая до цепи и
    упавшая там, всё равно писала fill: с confirmed=false, с суммой из котировки и
    нулевым количеством. В учёте она выглядела состоявшейся сделкой на $10, раздувала
    gross_usd и — главное — маскировала долю отказов: мой первый замер дал 22% вместо
    настоящих 35%, потому что считал провалом только отклонение на предполёте.

    Отдельный тип записи, а не отсутствие записи: попытка была, деньги на комиссию
    потрачены, и след этого нужен.
    """
    _append({"ts": _now(), "type": "reject", "intent_id": intent_id, "mode": mode,
             "token_mint": token, "reason": reason, "signature": signature, **(extra or {})})


def load(path=None) -> list[dict]:
    p = path or (config.OUTPUT_DIR / PATH)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def reconcile(rows: list[dict] | None = None) -> dict:
    """Сверка: у каждого intent должен быть fill; считаем расхождение цены и денег.

    Расхождение intent-цены и fill-цены = реальный slippage исполнения. В live это
    главный индикатор: если он систематически хуже shadow-замеров — модель врёт.
    """
    rows = rows if rows is not None else load()
    intents = {r["id"]: r for r in rows if r.get("type") == "intent"}
    fills = defaultdict(list)
    for r in rows:
        if r.get("type") == "fill":
            fills[r.get("intent_id")].append(r)

    orphan_fills = [r for r in rows if r.get("type") == "fill" and r.get("intent_id") not in intents]
    rejects = [r for r in rows if r.get("type") == "reject"]
    # намерение без исполнения = сделка не состоялась. Отклонённая на цепи попытка
    # тоже сюда: она пишет reject, а не fill (правка 10.08).
    unfilled = [i for iid, i in intents.items() if iid not in fills]
    slippages = []
    gross_usd = 0.0
    crossed = 0
    for iid, i in intents.items():
        for f in fills.get(iid, []):
            gross_usd += f.get("usd") or 0.0
            # ЧУЖАЯ ПАРА (аудит 10.08): до правки 05.08 исполнение ВЫХОДА подшивалось к
            # намерению на ПОКУПКУ — одно намерение на обе ноги сделки. Тогда fp/ip это
            # не проскальзывание, а доходность позиции, и в сверку попадали значения вроде
            # −92%. Таких пар в журнале 661 из 5331; они историчны и уже не появляются,
            # но продолжали портить цифру в каждом пульсе. Признак: у исполнения есть
            # reason (то есть это выход), а намерение — на покупку.
            if i.get("side") == "buy" and f.get("reason"):
                crossed += 1
                continue
            ip, fp = i.get("price"), f.get("price")
            if ip and fp and ip > 0:
                slippages.append(fp / ip - 1)
    import statistics
    return {
        "intents": len(intents),
        "fills": sum(len(v) for v in fills.values()),
        "unfilled": len(unfilled),            # намерения без исполнения (в live = провал tx!)
        "orphan_fills": len(orphan_fills),    # исполнение без намерения = ТРЕВОГА
        "rejects": len(rejects),              # попытки, не дошедшие до сделки
        "reject_rate": (len(unfilled) / len(intents)) if intents else 0.0,
        "crossed_legs": crossed,              # исторические пары «выход подшит к покупке»
        "median_slippage": statistics.median(slippages) if slippages else None,
        "worst_slippage": min(slippages) if slippages else None,
        "gross_usd": gross_usd,
        "ok": not orphan_fills,
    }


def summary() -> str:
    r = reconcile()
    # В БУМАЖНОМ РЕЖИМЕ проскальзывание тождественно нулю: цена намерения и цена
    # исполнения — одно и то же число. Показывать «медиана slippage +0.00%» как
    # достижение нельзя, это не измерение, а тавтология. Цифра оживёт только в live.
    sl = (f"{r['median_slippage']:+.2%}" if r["median_slippage"] is not None else "—")
    live = any(x.get("mode") == "live" for x in load() if x.get("type") == "fill")
    sl_txt = f"медиана slippage {sl}" if live else "slippage: нет живых сделок"
    flag = "✅" if r["ok"] else "🛑 ЕСТЬ ИСПОЛНЕНИЯ БЕЗ НАМЕРЕНИЙ"
    extra = f", старых кросс-пар {r['crossed_legs']}" if r["crossed_legs"] else ""
    отк = f" · отказов {r['rejects']} ({r['reject_rate']:.0%})" if r["rejects"] else ""
    return (f"{flag} леджер: намерений {r['intents']}, исполнений {r['fills']}, "
            f"без исполнения {r['unfilled']}{extra}{отк}, {sl_txt}")


if __name__ == "__main__":
    print(summary())
    for row in load()[-3:]:
        print(" ", {k: v for k, v in row.items() if k not in ("strategy_version",)})
