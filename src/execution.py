"""Фаза B — SHADOW-исполнение: измеряем РЕАЛЬНУЮ фрикцию, НЕ отправляя транзакций.

Зачем: единственный открытый вопрос проекта — реальный exit-хайркат. Бумажный edge
умирает, если round-trip фрикция > ~12% (аудит-3). Измерить её можно, не тратя ни доллара:
котируем покупку клипа и МГНОВЕННО обратную продажу того же количества токенов.

  buy:  X лампортов SOL → T сырых токенов   (Jupiter quote)
  sell: T сырых токенов → Y лампортов SOL   (Jupiter quote)
  фрикция round-trip = 1 − Y/X   ← искомое число (decimals сокращаются, знать их не нужно)

Плюс приоритетная комиссия (Helius) × 2 транзакции. НИЧЕГО не подписывается и не шлётся:
модуль умеет только читать котировки. Ключей кошелька в проекте нет вообще.

Self-test:  .venv\\Scripts\\python.exe -m src.execution <mint>
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import requests

from . import config, helius, market, strategy

JUP_QUOTE = "https://lite-api.jup.ag/swap/v1/quote"
WSOL = "So11111111111111111111111111111111111111112"
CU_PER_SWAP = 200_000          # типичный лимит вычислений на своп (для оценки priority fee)


def quote(input_mint: str, output_mint: str, amount_raw: int, slippage_bps: int,
          timeout: int = 15) -> dict | None:
    """Котировка Jupiter. None = маршрута нет (токен не торгуется/слишком ранний)."""
    try:
        r = requests.get(JUP_QUOTE, params={
            "inputMint": input_mint, "outputMint": output_mint,
            "amount": str(int(amount_raw)), "slippageBps": slippage_bps,
        }, timeout=timeout)
    except Exception:  # noqa: BLE001
        return None
    if r.status_code != 200:
        return None
    d = r.json()
    return d if d.get("outAmount") else None


def priority_fee_microlamports() -> float | None:
    """Рекомендованная приоритетная комиссия (микролампорты за единицу вычислений)."""
    try:
        r = helius.rpc("getPriorityFeeEstimate", [{
            "accountKeys": ["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
            "options": {"recommended": True}}])
        return (r.get("result") or {}).get("priorityFeeEstimate")
    except Exception:  # noqa: BLE001
        return None


def measure(mint: str, clip_usd: float | None = None, slippage_bps: int | None = None,
            sol_usd: float | None = None) -> dict:
    """Замер round-trip фрикции для клипа. Ничего не отправляет — только котировки."""
    ex = strategy.EXECUTION
    clip_usd = clip_usd if clip_usd is not None else ex["SHADOW_CLIP_USD"]
    slippage_bps = slippage_bps if slippage_bps is not None else ex["SLIPPAGE_BPS"]
    sol_usd = sol_usd or market.sol_price()
    res: dict = {"ts": datetime.now(timezone.utc).isoformat(), "mint": mint,
                 "clip_usd": clip_usd, "slippage_bps": slippage_bps, "sol_usd": sol_usd,
                 "routable": False}

    lamports_in = int(clip_usd / sol_usd * 1e9)
    t0 = time.time()
    buy = quote(WSOL, mint, lamports_in, slippage_bps)
    if not buy:
        res["error"] = "нет маршрута на покупку"
        return res
    tokens_raw = int(buy["outAmount"])
    sell = quote(mint, WSOL, tokens_raw, slippage_bps)
    res["quote_latency_s"] = round(time.time() - t0, 2)
    if not sell:
        res["error"] = "нет маршрута на продажу (ловушка ликвидности!)"
        res["buy_impact_pct"] = float(buy.get("priceImpactPct") or 0)
        return res

    lamports_out = int(sell["outAmount"])
    frict = 1 - lamports_out / lamports_in          # чистая фрикция round-trip
    pf = priority_fee_microlamports()
    # приоритетная комиссия: микролампорты/CU × CU × 2 транзакции → SOL → доля от клипа
    pf_sol = (pf * CU_PER_SWAP * 2 / 1e6 / 1e9) if pf else 0.0
    pf_frac = pf_sol * sol_usd / clip_usd if clip_usd else 0.0

    res.update({
        "routable": True,
        "tokens_raw": tokens_raw,
        "buy_impact_pct": float(buy.get("priceImpactPct") or 0),
        "sell_impact_pct": float(sell.get("priceImpactPct") or 0),
        "roundtrip_friction": frict,              # ГЛАВНОЕ ЧИСЛО (порог смерти edge ~0.12)
        "priority_fee_microlamports": pf,
        "priority_fee_frac": pf_frac,
        "total_cost": frict + pf_frac,            # фрикция + приоритетные комиссии
        "buy_route": [s["swapInfo"]["label"] for s in buy.get("routePlan", [])],
        "sell_route": [s["swapInfo"]["label"] for s in sell.get("routePlan", [])],
    })
    return res


def log_measure(rec: dict) -> None:
    with open(config.OUTPUT_DIR / "shadow_fills.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def measure_and_log(mint: str, phase: str, clip_usd: float | None = None) -> dict:
    """Замер + запись с меткой фазы ('entry' при открытии позиции, 'exit' при закрытии)."""
    rec = measure(mint, clip_usd)
    rec["phase"] = phase
    log_measure(rec)
    return rec


if __name__ == "__main__":
    import sys
    mints = sys.argv[1:] or [WSOL]
    sol = market.sol_price()
    pf = priority_fee_microlamports()
    print(f"SOL=${sol:.2f} · приоритетная комиссия: {pf} микролампорт/CU")
    for m in mints:
        r = measure(m, sol_usd=sol)
        if not r["routable"]:
            print(f"{m[:14]}… НЕ РОУТИТСЯ: {r.get('error')}")
            continue
        print(f"{m[:14]}… клип ${r['clip_usd']:.0f}: "
              f"фрикция {r['roundtrip_friction']:+.2%} + приоритет {r['priority_fee_frac']:.2%} "
              f"= ИТОГО {r['total_cost']:.2%} "
              f"(impact buy {r['buy_impact_pct']:.2%}/sell {r['sell_impact_pct']:.2%}, "
              f"маршрут {'+'.join(r['buy_route'][:2])}, {r['quote_latency_s']}с)")
