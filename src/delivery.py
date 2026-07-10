"""Модуль I — доставка сигнала: Telegram + PAPER-лог + быстрые ссылки.

Всегда пишет сигнал в output/signals.log (JSONL) и PAPER-позицию в output/paper_positions.jsonl.
Telegram шлёт, если в .env есть TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; иначе dry-run (печать).
Ключи читаются в рантайме, не логируются.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone

import requests

from . import config
from .signal_engine import Signal


def _links(mint: str) -> dict[str, str]:
    return {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",
        "solscan": f"https://solscan.io/token/{mint}",
        "gmgn": f"https://gmgn.ai/sol/token/{mint}",
        "jupiter": f"https://jup.ag/swap/SOL-{mint}",
    }


def format_message(sig: Signal, safety: dict) -> str:
    emoji = "🟢" if sig.level == "strong" else "🟡"
    sv = safety.get("verdict", "unknown")
    sv_emoji = {"ok": "✅", "warn": "⚠️", "danger": "⛔", "unknown": "❓"}.get(sv, "❓")
    lk = _links(sig.token_mint)
    risks = ", ".join(safety.get("risks", []) or []) or "—"
    return (
        f"{emoji} CONFLUENCE [{sig.level.upper()}] — {sig.n_actors} акторов\n"
        f"token: {sig.token_mint}\n"
        f"акторы: {', '.join(a[:8] for a in sig.actors)}\n"
        f"объём в окне: ${sig.window_usd:,} · сила: {sig.strength}\n"
        f"safety: {sv_emoji} {sv} ({risks})\n"
        f"📈 {lk['dexscreener']}\n🔎 {lk['solscan']}\n⚡ {lk['jupiter']}"
    )


def _append(path, obj) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def send_telegram(text: str) -> bool:
    token = config.secret("TELEGRAM_BOT_TOKEN", required=False)
    chat = config.secret("TELEGRAM_CHAT_ID", required=False)
    if not token or not chat:
        print("[telegram DRY-RUN] (нет TELEGRAM_* в .env)\n" + text)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                          json={"chat_id": chat, "text": text, "disable_web_page_preview": True},
                          timeout=15)
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        print(f"[telegram error] {type(e).__name__}: {e}")
        return False


def deliver(sig: Signal, safety: dict, entry_mc: float | None = None, paper: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rec = {"ts": now, "signal": asdict(sig), "safety_verdict": safety.get("verdict"),
           "risks": safety.get("risks"), "entry_mc": entry_mc}
    _append(config.OUTPUT_DIR / "signals.log", rec)
    if paper and safety.get("verdict") != "danger":   # PAPER не входим в danger
        _append(config.OUTPUT_DIR / "paper_positions.jsonl",
                {"ts": now, "token_mint": sig.token_mint, "level": sig.level,
                 "n_actors": sig.n_actors, "strength": sig.strength, "entry_mc": entry_mc,
                 "status": "open"})
    send_telegram(format_message(sig, safety))


def _demo() -> None:
    from .signal_engine import Signal as S
    sig = S(token_mint="FMqh9mqR6drPZqqW6wPqLHxX4rqNDWGhYLaMfoaJpump", ts=1000.0,
            actors=["actor1", "actor2", "actor3"], n_actors=3, window_usd=740,
            strength=23.74, level="strong", first_buy_ts=1000.0)
    from .safety import screen
    safety = screen(sig.token_mint)
    print("=== demo deliver (safety live + dry-run telegram + PAPER log) ===")
    deliver(sig, safety, entry_mc=60_000)
    print("\nsignals.log и paper_positions.jsonl обновлены в output/")


if __name__ == "__main__":
    _demo()
