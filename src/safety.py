"""Модуль H — safety-скрин токена (RugCheck free public API, без ключа).

Публичный RugCheck: без авторизации ~10 отчётов/мин. Возвращаем нормализованный вердикт
(ok / warn / danger) + список рисков. On-chain self-compute (LP burned, mint authority,
top-10) — можно добавить позже; для MVP опираемся на сводку RugCheck.

Run (тест на реальном mint):
  .venv\\Scripts\\python.exe -m src.safety <mint>
"""
from __future__ import annotations

import sys
import time

import requests

BASE = "https://api.rugcheck.xyz/v1"


def _fetch(mint: str, timeout: int) -> dict:
    url = f"{BASE}/tokens/{mint}/report/summary"
    try:
        r = requests.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except Exception as e:  # noqa: BLE001
        return {"verdict": "unknown", "error": f"{type(e).__name__}: {e}"}
    if r.status_code != 200:
        return {"verdict": "unknown", "http": r.status_code, "body": r.text[:200]}
    return {"data": r.json()}


def screen(mint: str, timeout: int = 20, retries: int = 1) -> dict:
    """Вернуть {verdict, score, risks, raw}. verdict: 'ok'|'warn'|'danger'|'unknown'.

    unknown = сеть/HTTP-сбой → один ретрай перед fail-open (не пускать позицию вслепую из-за
    случайного таймаута). Настоящий 'danger' от RugCheck ретраем НЕ трогаем.
    """
    res = _fetch(mint, timeout)
    for _ in range(retries):
        if "data" in res:
            break
        time.sleep(1)
        res = _fetch(mint, timeout)
    if "data" not in res:
        return {"verdict": "unknown", **{k: v for k, v in res.items() if k != "data"}}

    data = res["data"]
    risks = data.get("risks", []) or []
    levels = {str(x.get("level", "")).lower() for x in risks}
    if "danger" in levels:
        verdict = "danger"
    elif "warn" in levels:
        verdict = "warn"
    else:
        verdict = "ok"
    # concentration red-flag: ТЕКУЩАЯ концентрация холдеров (риск дампа) по рискам RugCheck.
    # ВНИМАНИЕ: это НЕ валидированный launch-инсайдер-маркер из ресерча (коллапс раннего hold-rate
    # к ATH) — тот пост-фактум, вживую не считается. RugCheck отражает СНИМОК сейчас (у ANSEM True,
    # у распределившегося к ATH USWR False). Годится как общий риск-флаг, не как edge-предиктор.
    _INS = ("holder", "concentrat", "top ", "insider", "supply owned", "single")
    insider = any(any(k in str(x.get("name", "")).lower() for k in _INS) for x in risks)
    return {
        "verdict": verdict,
        "score": data.get("score_normalised", data.get("score")),
        "risks": [f"{x.get('name')}({x.get('level')})" for x in risks],
        "insider": insider,
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
