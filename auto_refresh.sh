#!/bin/bash
# АВТОНОМНОЕ обновление watchlist: собирает discovery на Dune и применяет БЕЗ участия человека.
# Первый запуск 10.08 (сброс кредитов), далее каждые 7 дней. Итог всегда уходит в Telegram.
cd /opt/smart-money || exit 0
START=20260810
TODAY=$(date -u +%Y%m%d)
[ "$TODAY" -lt "$START" ] && exit 0          # до сброса кредитов не начинаем

STAMP=output/.last_auto_refresh
if [ -f "$STAMP" ]; then
  AGE=$(( ( $(date +%s) - $(stat -c %Y "$STAMP") ) / 86400 ))
  [ "$AGE" -lt 7 ] && exit 0                 # не чаще раза в 7 дней
fi

LOG=logs/auto_refresh_$(date -u +%Y%m%d).log
mkdir -p logs
echo "=== $(date -u) старт автообновления ===" >> "$LOG"
# СНИМОК ПРЕЖНЕГО СПИСКА до запуска discovery: он перезапишет flow_watchlist.json
# своими кандидатами, а нам нужно с чем сливать (аудит 09.08 — замена вместо
# слияния стоила бы 3831 сделки и 4153$).
cp output/flow_watchlist.json output/flow_watchlist_prev.json 2>/dev/null

OUT=$(docker compose run --rm discovery 2>&1)
echo "$OUT" >> "$LOG"
touch "$STAMP"

# СЛИЯНИЕ: кандидаты discovery + акторы, проверенные нашими живыми сделками.
# Выбрасываются только те, чьё молчание подтверждено наблюдением.
if echo "$OUT" | grep -q REFRESH_OK; then
  MERGE=$(docker compose run --rm discovery python -m src.merge_watchlist 2>&1)
  echo "$MERGE" >> "$LOG"
  if ! echo "$MERGE" | grep -q "MERGE_OK"; then
    # слияние не удалось — откатываемся на прежний список, а не торгуем по куцему
    cp output/flow_watchlist_prev.json output/flow_watchlist.json 2>/dev/null
    echo "СЛИЯНИЕ НЕ УДАЛОСЬ — восстановлен прежний список" >> "$LOG"
  fi
fi

if echo "$OUT" | grep -q REFRESH_OK; then
  # список применяется только после перезапуска: монитор читает watchlist на старте
  docker compose restart monitor >> "$LOG" 2>&1
  echo "монитор перезапущен" >> "$LOG"
fi
