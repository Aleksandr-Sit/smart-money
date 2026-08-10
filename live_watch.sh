#!/bin/bash
# ЕЖЕЧАСНЫЙ КОНТРОЛЬ ЖИВОЙ ТОРГОВЛИ. Молчит, когда всё в порядке.
#
# Зачем отдельно от пульса монитора: пульс раз в 6 часов и рассказывает, что бот
# ЖИВ. Здесь проверяется другое — сходятся ли деньги. Пульс не заметит ни сорванных
# покупок, ни застрявших токенов, ни расхождения между реализованным PnL и фактическим
# движением SOL на кошельке.
cd /opt/smart-money || exit 0
LOG=logs/live_watch.log
mkdir -p logs
STATE=output/.live_watch_state

# Счётчики сорванных сделок берём ЗДЕСЬ: внутри контейнера нет docker CLI.
FAIL_BUY=$(docker logs --since 70m smart-money-monitor 2>&1 | grep -ac "LIVE BUY FAIL")
FAIL_SELL=$(docker logs --since 70m smart-money-monitor 2>&1 | grep -ac "LIVE SELL FAIL")
DUP=$(docker logs --since 70m smart-money-monitor 2>&1 | grep -ac "EXIT dup")
TRACE=$(docker logs --since 70m smart-money-monitor 2>&1 | grep -ac "Traceback")

docker compose exec -T -e FAIL_BUY="$FAIL_BUY" -e FAIL_SELL="$FAIL_SELL" \
  -e DUP="$DUP" -e TRACE="$TRACE" monitor python - <<'PYEOF' >> "$LOG" 2>&1
import json, os, subprocess, sys, time
sys.path.insert(0, "/app")
from src import delivery, orphans, positions, strategy, wallet

ПРОБЛЕМЫ = []
СОСТ = "/app/output/.live_watch_state"

def prev():
    try:
        return json.load(open(СОСТ))
    except Exception:
        return {}

было = prev()
now = time.time()

# --- 1. кошелёк ---
w = wallet.Wallet()
sol = w.balance_sol()
if sol is None:
    ПРОБЛЕМЫ.append("баланс кошелька НЕ ЧИТАЕТСЯ — узел молчит")
elif sol < 0.2:
    ПРОБЛЕМЫ.append(f"на кошельке всего {sol:.3f} SOL — торговать скоро будет нечем")

# --- 2. счётчик риска и дневной стоп ---
rs = json.load(open("/app/output/risk_state.json"))
лимит = -strategy.RISK["DAILY_STOP_FRAC"] * strategy.RISK["BANKROLL_USD"]
if rs.get("halted"):
    ПРОБЛЕМЫ.append(f"ТОРГОВЛЯ ОСТАНОВЛЕНА: {rs.get('halt_reason')}")
elif rs["realized_usd"] <= лимит * 0.7:
    ПРОБЛЕМЫ.append(f"дневной убыток ${rs['realized_usd']:.2f} подходит к стопу ${лимит:.0f}")

# --- 3. сироты: токены без позиции ---
try:
    r = orphans.scan()
    if r["orphan"]:
        ПРОБЛЕМЫ.append(f"СИРОТ {len(r['orphan'])} на ${r['orphan_usd']:.2f} — "
                        f"токены без позиции: {', '.join(a['mint'][:10] for a in r['orphan'][:3])}")
    if len(r["empty"]) > 5:
        ПРОБЛЕМЫ.append(f"незакрытых пустых аккаунтов {len(r['empty'])} — рента заморожена")
except Exception as e:
    ПРОБЛЕМЫ.append(f"осмотр кошелька не удался: {type(e).__name__}")

# --- 4. сорванные сделки за последний час (счётчики пришли с хоста) ---
сорвано_buy = int(os.environ.get("FAIL_BUY", 0))
сорвано_sell = int(os.environ.get("FAIL_SELL", 0))
if сорвано_sell:
    ПРОБЛЕМЫ.append(f"СОРВАННЫХ ПРОДАЖ за час: {сорвано_sell} — токен мог остаться в кошельке")
if сорвано_buy > 5:
    ПРОБЛЕМЫ.append(f"сорванных покупок за час: {сорвано_buy}")
if int(os.environ.get("TRACE", 0)):
    ПРОБЛЕМЫ.append(f"НЕОБРАБОТАННЫХ ИСКЛЮЧЕНИЙ в логе за час: {os.environ['TRACE']}")
дублей = int(os.environ.get("DUP", 0))

# --- 5. расхождение: реализованный PnL против фактического движения SOL ---
# Главная проверка. Бумажный учёт и кошелёк обязаны сходиться; расхождение означает,
# что бот считает не то, что происходит с деньгами.
if sol is not None and было.get("sol") is not None and было.get("realized") is not None:
    факт = sol - было["sol"]                       # сколько SOL реально прибавилось
    учёт_usd = rs["realized_usd"] - было["realized"]
    from src import market
    учёт = учёт_usd / market.sol_price()
    откр = len(positions.PositionManager().open_tokens())
    # открытые позиции лежат в токенах, поэтому допуск = клип × число позиций + запас
    допуск = (откр + 1) * strategy.RISK["CLIP_USD"] / market.sol_price() + 0.02
    if abs(факт - учёт) > допуск:
        ПРОБЛЕМЫ.append(
            f"РАСХОЖДЕНИЕ: кошелёк изменился на {факт:+.4f} SOL, учёт говорит "
            f"{учёт:+.4f} SOL (открыто позиций {откр}, допуск {допуск:.4f})")

json.dump({"sol": sol, "realized": rs["realized_usd"], "ts": now}, open(СОСТ, "w"))

метка = time.strftime("%Y-%m-%d %H:%M", time.gmtime())
if ПРОБЛЕМЫ:
    txt = f"КОНТРОЛЬ ЖИВОЙ ТОРГОВЛИ {метка} UTC\n" + "\n".join("• " + p for p in ПРОБЛЕМЫ)
    print(txt)
    delivery.send_alert(txt)
else:
    print(f"{метка} всё сходится · {sol:.4f} SOL · день ${rs['realized_usd']:+.2f} "
          f"· сделок {rs['n_trades']} · отложенных дублей выхода {дублей}")
PYEOF
