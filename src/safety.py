"""Модуль H — safety-скрин токена (RugCheck free public API, без ключа).

Публичный RugCheck: без авторизации ~10 отчётов/мин. Возвращаем нормализованный вердикт
(ok / warn / danger) + список рисков. On-chain self-compute (LP burned, mint authority,
top-10) — можно добавить позже; для MVP опираемся на сводку RugCheck.

Run (тест на реальном mint):
  .venv\\Scripts\\python.exe -m src.safety <mint>
"""
from __future__ import annotations

import sys

import requests

BASE = "https://api.rugcheck.xyz/v1"


def screen(mint: str, timeout: int = 20) -> dict:
    """Вернуть {verdict, score, risks, raw}. verdict: 'ok'|'warn'|'danger'|'unknown'."""
    url = f"{BASE}/tokens/{mint}/report/summary"
    try:
        r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unknown", "error": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"verdict": "unknown", "http": r.status_code, "body": r.text[:200]}

    data = r.json()
    risks = data.get("risks", []) or []
    levels = {str(x.get("level", "")).lower() for x in risks}
    if "danger" in levels:
        verdict = "danger"
    elif "warn" in levels:
        verdict = "warn"
    else:
        verdict = "ok"
    return {
        "verdict": verdict,
        "score": data.get("score_normalised", data.get("score")),
        "risks": [f"{x.get('name')}({x.get('level')})" for x in risks],
        "raw": data,
    }


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("usage: -m src.safety <mint>")
    for mint in sys.argv[1:]:
        res = screen(mint)
        print(f"\n{mint}")
        print(f"  verdict: {res['verdict']}  score: {res.get('score')}")
        print(f"  risks:   {res.get('risks')}")
        if res["verdict"] == "unknown":
            print(f"  debug:   {({k: v for k, v in res.items() if k not in ('raw',)})}")


if __name__ == "__main__":
    main()
