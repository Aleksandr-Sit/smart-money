"""Dune schema discovery before writing Module A (English output to avoid console mojibake).

Goals:
  1. Confirm run_sql works on the current tier (already OK) with a higher timeout.
  2. Find the Solana pump.fun decoded trade table + its columns (metadata = cheap).
  3. Check dex_solana.trades data lag and whether pumpfun/pumpswap appear as projects.

The API key is read from .env at runtime and is NEVER printed.
Run:  .venv\\Scripts\\python.exe src\\probe_dune.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

API_KEY = os.getenv("DUNE_API_KEY")
if not API_KEY:
    sys.exit("DUNE_API_KEY not found in .env")

from dune_client.client import DuneClient  # noqa: E402

dune = DuneClient(API_KEY, request_timeout=120)


def run(sql: str, label: str, limit_print: int = 80):
    print(f"\n=== {label} ===")
    try:
        res = dune.run_sql(query_sql=sql, is_private=True)
        rows = res.get_rows()
        print(f"OK, rows: {len(rows)}")
        for row in rows[:limit_print]:
            print("  ", row)
        return rows
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        return None


def main():
    # 1. Solana pump.fun decoded tables (metadata, cheap)
    run(
        """
        SELECT table_schema, table_name
        FROM information_schema.tables
        WHERE table_schema LIKE '%solana%'
          AND (lower(table_schema) LIKE '%pump%')
        ORDER BY 1, 2
        LIMIT 300
        """,
        "1) Solana pump.* schemas/tables",
    )

    # 2. Columns of pump.fun Solana TRADE-like tables (metadata, cheap)
    run(
        """
        SELECT table_schema, table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema LIKE '%solana%'
          AND lower(table_schema) LIKE '%pump%'
          AND (lower(table_name) LIKE '%trade%' OR lower(table_name) LIKE '%buy%'
               OR lower(table_name) LIKE '%swap%')
        ORDER BY table_schema, table_name, ordinal_position
        LIMIT 300
        """,
        "2) Columns of pump.fun Solana trade/buy/swap tables",
    )

    # 3. dex_solana.trades: lag + which projects appear (one modest scan, 12h)
    run(
        """
        SELECT project, count(*) AS n, max(block_time) AS latest
        FROM dex_solana.trades
        WHERE block_time > now() - interval '12' hour
        GROUP BY 1
        ORDER BY 2 DESC
        LIMIT 40
        """,
        "3) dex_solana.trades projects (12h) + latest block_time",
    )


if __name__ == "__main__":
    main()
