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
# СНИМОК ПРЕЖНЕГО СПИСКА — страховка на случай, если контейнер умрёт между записью
# discovery и слиянием. Само слияние живёт ВНУТРИ src.auto_refresh (правка 10.08):
# так отчёт в Telegram описывает итоговый список, а не промежуточный результат
# discovery, и не шлёт ложное «приоритетный актор выбыл».
cp output/flow_watchlist.json output/flow_watchlist_prev.json 2>/dev/null

# Имена переменных ТОЛЬКО ASCII: bash не допускает кириллицу в идентификаторах,
# и скрипт падал на первой же строке присваивания (поймано 12.08).
# УБИРАЕМ ХВОСТЫ ПРЕДЫДУЩИХ ЗАПУСКОВ (найдено 12.08). Прогон discovery идёт больше
# двадцати минут; если он подвис или его оборвали, контейнер остаётся жив и держит
# блокировку файла DuckDB. Следующий недельный запуск падает на
# «Conflicting lock is held», причём молча. Именно так автообновление не срабатывало ни разу.
STALE=$(docker ps -q --filter "name=smart-money-discovery-run-")
if [ -n "$STALE" ]; then
  echo "убираю зависшие контейнеры discovery: $STALE" >> "$LOG"
  docker rm -f $STALE >> "$LOG" 2>&1
fi

# Потолок по времени: подвисший прогон не должен дожить до следующей недели и
# заблокировать её. 45 минут — вчетверо больше наблюдавшегося нормального прогона.
OUT=$(timeout 2700 docker compose run --rm discovery 2>&1)
RC=$?
echo "$OUT" >> "$LOG"
[ "$RC" = 124 ] && echo "ПРЕВЫШЕН ЛИМИТ 45 МИН — прогон оборван по таймауту" >> "$LOG"
touch "$STAMP"

if ! echo "$OUT" | grep -q REFRESH_OK; then
  # обновление не дошло до конца — вернуть заведомо рабочий список и не трогать монитор
  cp output/flow_watchlist_prev.json output/flow_watchlist.json 2>/dev/null
  echo "ОБНОВЛЕНИЕ НЕ УДАЛОСЬ — восстановлен прежний список" >> "$LOG"
fi

if echo "$OUT" | grep -q REFRESH_OK; then
  # список применяется только после перезапуска: монитор читает watchlist на старте
  docker compose restart monitor >> "$LOG" 2>&1
  echo "монитор перезапущен" >> "$LOG"
fi
