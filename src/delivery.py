"""Модуль I — доставка сигнала: Telegram + PAPER-лог + быстрые ссылки.

Всегда пишет сигнал в output/signals.log (JSONL) и PAPER-позицию в output/paper_positions.jsonl.
Telegram шлёт, если в .env есть TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID; иначе dry-run (печать).
Ключи читаются в рантайме, не логируются.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone

import requests

from . import config, strategy
from .signal_engine import Signal

EXIT_FEE = strategy.RISK["EXIT_FEE"]   # round-trip swap+priority для realized_net (единый конфиг)


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
    # маркер КАЧЕСТВА входа. ВНИМАНИЕ (аудит-4): робастен только на live-исходах, на
    # траекториях затухает (1пол +43.7% → 2пол +7.8%) → МАРКЕР, не гейт. Пороги — из конфига.
    quality = ((info.get("mc") or 0) >= strategy.ALERTS["QUALITY_MIN_MC"]
               and (info.get("buys_h1") or 0) >= strategy.ALERTS["QUALITY_MIN_VELOCITY"])
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
            paper: bool = True, telegram: bool = True,
            независимых: int | None = None) -> None:
    """`независимых` — сколько РАЗНЫХ участников стоит за сигналом (см. `lockstep`).

    Замер 11.08: `n_actors` завышает подтверждение, потому что один участник держит
    несколько кошельков и в одиночку закрывает порог CONFLUENCE_N=2. Так возникали
    12.3% сигналов вообще без независимого подтверждения. Пишем поле в журнал, чтобы
    разделение считалось по факту, а не восстанавливалось задним числом.
    """
    now = datetime.now(timezone.utc).isoformat()
    info = info or {}
    rec = {"ts": now, "strategy_version": strategy.VERSION,   # каким конфигом порождён сигнал
           "signal": asdict(sig), "safety_verdict": safety.get("verdict"),
           "risks": safety.get("risks"), "insider": safety.get("insider"), "market": info,
           "независимых": независимых}
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
               "stop_loss": "стоп-лосс", "trailing": "трейлинг от пика", "dead": "токен мёртв",
               # добавлены 10.08: три причины уходили в Telegram как сырые английские
               # слова, хотя timeout — вторая по частоте причина выхода вообще
               "timeout": "истекло время удержания", "lost_price": "цена потеряна",
               "orphan": "подобран без позиции"}


def format_exit(pos, exit_price: float, reason: str, realized_pnl: float) -> str:
    """Сообщение о выходе. Включает параметры ВХОДА намеренно.

    С 07.08 правило входа "all" — торгуем каждый сигнал, но уведомления о входе
    уходят только для отобранных классов. Возникла асимметрия: объявляем все
    выходы и лишь часть входов, из-за чего сообщение о выходе выглядит сиротой,
    и владелец не может найти, когда и по чему мы вошли (жалоба 08.08).
    Слать вход по каждой сделке нельзя — это ~200 сообщений в сутки, поэтому
    пара восстанавливается прямо здесь.
    """
    lk = _links(pos.token_mint)
    emoji = "🟢" if realized_pnl >= 0 else "🔴"
    held = (time.time() - pos.entry_ts) if pos.entry_ts else None
    held_txt = ("?" if held is None else
                f"{held/60:.0f} мин" if held >= 60 else f"{held:.0f} с")
    entered = (datetime.fromtimestamp(pos.entry_ts, timezone.utc).strftime("%H:%M:%S")
               if pos.entry_ts else "?")

    def _x(p):
        return f"{p/pos.entry_price:.2f}x" if (p and pos.entry_price) else "?"

    lines = [
        f"🔻 EXIT [{_REASON_TXT.get(reason, reason)}] {emoji} realized {realized_pnl:+.0%}",
        f"token: {pos.token_mint}",
        f"ВХОД {entered} UTC · акторов {len(pos.entry_actors)} · MC {_fmt_usd(pos.entry_mc)}",
        f"держали {held_txt} · пик {_x(pos.peak_price)} · выход {_x(exit_price)}",
        f"вышло акторов: {len(pos.exited_actors)}/{len(pos.entry_actors)}",
        f"выход MC ~{_fmt_usd((exit_price or 0) * 1_000_000_000)}",
        f"📈 {lk['dexscreener']}",
    ]
    return "\n".join(lines)


def deliver_exit(pos, exit_price: float, reason: str, telegram: bool = True,
                 realized_actual: float | None = None,
                 exec_gap: float | None = None) -> None:
    """realized_actual — итог ПО ДЕНЬГАМ (живой режим). Если задан, он и есть правда.

    Модельный результат при этом сохраняется отдельным полем: сравнение двух чисел —
    единственный способ видеть цену исполнения. Замер 10.08 на 52 живых сделках:
    модель +$99.10, деньги +$37.63, разрыв −9.8% медиана на сделку.
    """
    now = datetime.now(timezone.utc).isoformat()
    from .positions import total_realized
    # итоговый GROSS PnL с учётом ЧАСТИЧНЫХ тейков (реализованное + остаток по цене выхода)
    model = total_realized(pos, exit_price)
    realized = realized_actual if realized_actual is not None else model
    # при фактическом итоге комиссии УЖЕ вычтены — они внутри полученных денег
    realized_net = realized if realized_actual is not None else realized - EXIT_FEE
    # mode ОБЯЗАТЕЛЕН (10.08): без него живые и бумажные выходы в одном файле
    # неразличимы, и первая же неделя живой торговли испортит всю накопленную
    # статистику — разделить постфактум будет нечем.
    rec = {"ts": now, "type": "exit", "strategy_version": strategy.VERSION,
           "mode": "live" if strategy.EXECUTION["LIVE_ENABLED"] else "paper",
           "token_mint": pos.token_mint, "reason": reason,
           "entry_price": pos.entry_price, "exit_price": exit_price, "realized_pnl": realized,
           "realized_model": model, "exec_gap": exec_gap,
           "pnl_source": "деньги" if realized_actual is not None else "модель",
           "realized_net": realized_net, "realized_partial": pos.realized, "remaining": pos.remaining,
           "entry_actors": len(pos.entry_actors), "exited_actors": len(pos.exited_actors),
           "entry_ts": pos.entry_ts, "entry_mc": pos.entry_mc}
    _append(config.OUTPUT_DIR / "signals.log", rec)
    _append(config.OUTPUT_DIR / "paper_closed.jsonl", rec)
    if telegram:
        send_telegram(format_exit(pos, exit_price, reason, realized if realized is not None else 0.0))


def log_partial(pos, price: float | None, frac: float) -> None:
    """Лог частичного тейка (позиция продолжается с меньшим остатком)."""
    now = datetime.now(timezone.utc).isoformat()
    mult = (price / pos.entry_price) if (price and pos.entry_price) else None
    _append(config.OUTPUT_DIR / "paper_closed.jsonl",
            {"ts": now, "type": "partial", "token_mint": pos.token_mint, "frac": frac,
             "mult": mult, "remaining": pos.remaining, "realized_partial": pos.realized,
             "entry_ts": pos.entry_ts})


def log_actor_buy(actor: str, wallet: str, token: str, usd: float, ts: float,
                  converged: bool, n_actors: int, price: float | None = None,
                  curve_price: float | None = None) -> None:
    """Лог КАЖДОЙ покупки watchlist-кошелька — сошлась она в сигнал или нет.

    ЦЕНА ДОБАВЛЕНА 11.08 и это ключевое поле. Аудит входа показал, что каждый лишний
    актор стоит денег: на 2901 токене, дошедшем до трёх акторов, вход на втором дал
    +$7129 против −$1866 на третьем, а сам переход 2→3 занимает 29 секунд и делает
    вход дороже на 8.4%. Логично, что вход по ПЕРВОМУ актору ещё дешевле — но
    проверить это было нечем: трекер цены регистрирует токен только после того, как
    сложился конфлюенс, а здесь цена не записывалась.
    Теперь для любого токена, который потом сложится в сигнал, будет известна цена
    его самой первой покупки — то есть станет измеримым единственный неизмеренный
    вариант входа. Экстраполировать вместо замера на реальных деньгах нельзя.

    ЗАЧЕМ (аудит 10.08). До этого активность актора фиксировалась только когда он
    попадал в СИГНАЛ, то есть сходился с кем-то ещё в окне конфлюенса. Одиночные
    покупки не сохранялись нигде. Из-за этого испытательный срок нового актора
    невозможно было закрыть по существу: через 14 дней мы знали бы лишь «сигналов
    не было» и не отличили бы «перестал торговать» от «торгует, но не сходится с
    нашими». Первое — повод исключить, второе — повод искать ему напарника или
    признать, что просело покрытие рынка.

    Поле converged и есть та самая разница: покупка была, сигнала не вышло.
    """
    _append(config.OUTPUT_DIR / "actor_buys.jsonl",
            {"ts": ts, "actor": actor, "wallet": wallet, "token_mint": token,
             "usd": round(usd, 2), "price": price,
           # ЦЕНА КРИВОЙ В МОМЕНТ СДЕЛКИ — из того же события pump.fun, даром
           # (12.08). Прежде за ней ходили отдельным getAccountInfo, и ответ приходил
           # секундами позже: на свежем токене это уже другая цена. Теперь разрыв
           # «цена актора против рынка» измерим точно и на каждой покупке.
           "curve_price": curve_price,
             "converged": converged, "n_actors": n_actors})


def log_actor_sell_any(token: str, actor: str, price: float | None, ts) -> None:
    """Продажа актора по ОТСЛЕЖИВАЕМОМУ токену, в котором у нас позиции НЕТ.

    ЗАЧЕМ (10.08, вопрос владельца о работе не в полную силу). Когда бот держит меньше
    позиций, чем приходит сигналов, невзятые сигналы всё равно логируются, и трекер
    ведёт их цену — исход можно восстановить симуляцией. Но выход по продаже актора
    закрывает 86% позиций, а log_actor_sell пишет только при ОТКРЫТОЙ позиции. Без
    этого журнала у невзятых сигналов не было бы главного правила выхода, и
    восстановление опиралось бы на одни ценовые правила, решающие меньшинство случаев.

    Отдельный файл, а не общий: здесь нет цены входа и состава акторов позиции —
    их надо доставать из signals.log по токену.
    """
    _append(config.OUTPUT_DIR / "actor_sells_all.jsonl",
            {"ts": ts, "token_mint": token, "actor": actor, "sell_price": price})


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


def send_alert(text: str) -> bool:
    """Алерт качества данных/потока. Баг цены (аудит-4) жил недели незамеченным — больше нет."""
    return send_telegram("⚠️ АЛЕРТ: " + text)


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
