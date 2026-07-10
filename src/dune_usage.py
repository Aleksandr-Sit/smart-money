"""Проверка остатка кредитов Dune (метаданные, кредиты НЕ тратит).

Ключ читается из .env и НИКОГДА не печатается.
Run:  .venv\\Scripts\\python.exe -m src.dune_usage
"""
from __future__ import annotations

import json

import requests

from .config import secret


def main() -> None:
    key = secret("DUNE_API_KEY")
    url = "https://api.dune.com/api/v1/usage"
    headers = {"X-DUNE-API-KEY": key, "Content-Type": "application/json"}

    for method in ("POST", "GET"):
        try:
            r = requests.request(method, url, headers=headers, json={}, timeout=30)
        except Exception as e:  # noqa: BLE001
            print(f"{method} error: {type(e).__name__}: {e}")
            continue
        print(f"{method} -> HTTP {r.status_code}")
        if r.status_code == 200:
            print(json.dumps(r.json(), indent=2, ensure_ascii=False))
            return
        else:
            print("  ", r.text[:500])
    print("\nНе удалось получить usage через API — можно глянуть на dune.com/settings/billing.")


if __name__ == "__main__":
    main()
