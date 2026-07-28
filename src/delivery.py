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

EXIT_FEE = 0.06   # round-trip swap+priority для realized_net (хайркат slippage/реверсии сверх — не логируем)


def _links(mint: str) -> dict[str, str]:
    return {
        "dexscreener": f"https://dexscreener.com/solana/{mint}",   # график (смотреть)
        "gmgn": f"https://gmgn.ai/sol/token/{mint}",               # покупка (pump.fun вкл. кривую)
        "bullx": f"https://neo.bullx.io/terminal?address={mint}&chainId=1399811149",  # token deep-link
    }


def _fmt_usd(v) -> str:
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "—"


def format_message(sig: Signal, safety: dict, info: dict | None = None) -> str:
    emoji = "🟢" if sig.level == "strong" else "🟡"
    # класс конвикции: 🔇 тихий (лучший по ресерчу) / 📢 громкий (FOMO-пик, исторически хуже)
    quiet_tag = "🔇 ТИХИЙ" if getattr(sig, "quiet", False) else "📢 громкий"
    sv = safety.get("verdict", "unknown")
    sv_emoji = {"ok": "✅", "warn": "⚠️", "danger": "⛔", "unknown": "❓"}.get(sv, "❓")
    insider_tag = " · 🚩инсайдер-концентрация" if safety.get("insider") else ""
    lk = _links(sig.token_mint)
    risks = ", ".join(safety.get("risks", []) or []) or "—"
    info = info or {}
    # маркер КАЧЕСТВА входа (аудит-2: MC≥15k+velocity≥40 → win 58% mean +43%, оба time-split >0).
    # Пока МАРКЕР, не гейт — собираем forward-OOS.
    quality = (info.get("mc") or 0) >= 15000 and (info.get("buys_h1") or 0) >= 40
    quality_tag = " ⭐КАЧЕСТВО" if quality else ""
    market_line = ""
    if info:
        market_line = (f"MC {_fmt_usd(info.get('mc'))} · liq {_fmt_usd(info.get('liquidity_usd'))} · "
                       f"buys(1h) {info.get('buys_h1', '—')} (velocity){quality_tag}\n")
    return (
        f"{emoji} CONFLUENCE [{sig.level.upper()}] {quiet_tag} — {sig.n_actors} акторов\n"
        f"token: {sig.token_mint}\n"
        f"акторы: {', '.join(a[:8] for a in sig.actors)}\n"
        f"объём в окне: ${sig.window_usd:,} · сила: {sig.strength} · набор {getattr(sig, 'first_gap_s', 0):.0f}с\n"
        f"{market_line}"
        f"safety: {sv_emoji} {sv} ({risks}){insider_tag}\n"
        f"📈 график: {lk['dexscreener']}\n"
        f"⚡ GMGN: {lk['gmgn']}\n"
        f"⚡ BullX: {lk['bullx']}"
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


def deliver(sig: Signal, safety: dict, info: dict | None = None,
            paper: bool = True, telegram: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    info = info or {}
    rec = {"ts": now, "signal": asdict(sig), "safety_verdict": safety.get("verdict"),
           "risks": safety.get("risks"), "insider": safety.get("insider"), "market": info}
    _append(config.OUTPUT_DIR / "signals.log", rec)          # лог ВСЕГДА (для анализа)
    if paper and safety.get("verdict") != "danger":          # PAPER не входим в danger
        _append(config.OUTPUT_DIR / "paper_positions.jsonl",
                {"ts": now, "token_mint": sig.token_mint, "level": sig.level,
                 "n_actors": sig.n_actors, "strength": sig.strength,
                 "entry_mc": info.get("mc"), "entry_price_usd": info.get("price_usd"),
                 "velocity_buys_h1": info.get("buys_h1"), "status": "open"})
    if telegram:                                             # алерт только по решению вызывающего
        send_telegram(format_message(sig, safety, info))


_REASON_TXT = {"actors_exit": "акторы вышли", "take_profit": "тейк-профит",
               "stop_loss": "стоп-лосс", "trailing": "трейлинг от пика", "dead": "токен мёртв"}


def format_exit(pos, exit_price: float, reason: str, realized_pnl: float) -> str:
    lk = _links(pos.token_mint)
    emoji = "🟢" if realized_pnl >= 0 else "🔴"
    return (
        f"🔻 EXIT [{_REASON_TXT.get(reason, reason)}] {emoji} realized {realized_pnl:+.0%}\n"
        f"token: {pos.token_mint}\n"
        f"вышло акторов: {len(pos.exited_actors)}/{len(pos.entry_actors)}\n"
        f"вход MC {_fmt_usd(pos.entry_mc)} → выход ~{_fmt_usd((exit_price or 0) * 1_000_000_000)}\n"
        f"📈 {lk['dexscreener']}"
    )


def deliver_exit(pos, exit_price: float, reason: str, telegram: bool = True) -> None:
    now = datetime.now(timezone.utc).isoformat()
    realized = (exit_price / pos.entry_price - 1) if (pos.entry_price and exit_price) else None
    # realized_pnl — GROSS (историческая совместимость). realized_net — за вычетом round-trip
    # комиссии (swap+priority ~6%). Хайркат slippage/реверсии/сэндвича вживую не знаем → не в лог.
    realized_net = (realized - EXIT_FEE) if realized is not None else None
    rec = {"ts": now, "type": "exit", "token_mint": pos.token_mint, "reason": reason,
           "entry_price": pos.entry_price, "exit_price": exit_price, "realized_pnl": realized,
           "realized_net": realized_net,
           "entry_actors": len(pos.entry_actors), "exited_actors": len(pos.exited_actors),
           "entry_ts": pos.entry_ts, "entry_mc": pos.entry_mc}
    _append(config.OUTPUT_DIR / "signals.log", rec)
    _append(config.OUTPUT_DIR / "paper_closed.jsonl", rec)
    if telegram:
        send_telegram(format_exit(pos, exit_price, reason, realized if realized is not None else 0.0))


def log_actor_sell(token: str, actor: str, price: float | None, ts, pos) -> None:
    """Лог КАЖДОЙ продажи зашедшего актора по открытой позиции (не только триггерной).
    Открывает оптимизацию exit-правила: бэктест EXIT_ACTOR_FRAC по фактической
    последовательности продаж (какой актор по счёту вышел, по какой цене, PnL к тому моменту).
    """
    rec = {"ts": ts, "token_mint": token, "actor": actor, "sell_price": price,
           "entry_price": pos.entry_price, "entry_ts": pos.entry_ts,
           "n_entry_actors": len(pos.entry_actors),
           "n_exited_before": len(pos.exited_actors),
           "pnl_at_sell": (price / pos.entry_price - 1) if (pos.entry_price and price) else None}
    _append(config.OUTPUT_DIR / "actor_sells.jsonl", rec)


def send_heartbeat(text: str) -> bool:
    """Пульс: подтверждает, что монитор жив. Тишина в Telegram ≠ 'нет сигналов'."""
    return send_telegram("💓 " + text)


def _demo() -> None:
    from .signal_engine import Signal as S
    sig = S(token_mint="FMqh9mqR6drPZqqW6wPqLHxX4rqNDWGhYLaMfoaJpump", ts=1000.0,
            actors=["actor1", "actor2", "actor3"], n_actors=3, window_usd=740,
            strength=23.74, level="strong", first_buy_ts=1000.0)
    from .safety import screen
    from .market import token_info
    safety = screen(sig.token_mint)
    info = token_info(sig.token_mint)
    print("=== demo deliver (safety+market live + telegram + PAPER log) ===")
    deliver(sig, safety, info=info)
    print("\nsignals.log и paper_positions.jsonl обновлены в output/")


if __name__ == "__main__":
    _demo()
