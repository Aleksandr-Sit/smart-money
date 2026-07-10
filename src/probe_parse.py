"""Валидация tx_parse на реальных недавних сделках watchlist-кошельков.

Run:  .venv\\Scripts\\python.exe -m src.probe_parse <wallet> [<wallet> ...]
"""
from __future__ import annotations

import sys

from . import helius, tx_parse


def main() -> None:
    for w in sys.argv[1:]:
        sigs = helius.rpc("getSignaturesForAddress", [w, {"limit": 20}]).get("result", []) or []
        buys = 0
        print(f"\n{w}  сигнатур: {len(sigs)}")
        for s in sigs:
            if s.get("err"):
                continue
            buy = tx_parse.parse_buy(s["signature"], w)
            if buy:
                buys += 1
                print(f"  BUY token={buy['token_mint'][:12]}.. base={buy['base_amount']:.2f} "
                      f"sol={buy['sol_spent']:.4f} ts={buy['ts']}")
        print(f"  -> покупок распознано: {buys}")


if __name__ == "__main__":
    main()
