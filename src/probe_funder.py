"""Образец колонок system_program_call_transfer (LIMIT 5 = дёшево)."""
from __future__ import annotations

from . import dune_fetch


def main():
    rows = dune_fetch.run_sql(
        "SELECT * FROM system_program_solana.system_program_call_transfer LIMIT 5")
    if rows:
        print("COLUMNS:", list(rows[0].keys()))
        print()
        for r in rows:
            print(r)


if __name__ == "__main__":
    main()
