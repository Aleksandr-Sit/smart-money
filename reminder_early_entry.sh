#!/bin/bash
# ЗАМЕР ВХОДА ПО ПЕРВОМУ АКТОРУ через 72 часа после начала записи цены (11.08 07:20 UTC).
#
# Присылает не напоминание, а РЕЗУЛЬТАТ: скрипт сам считает парное сравнение и шлёт
# итог в Telegram. Напоминание «посмотри выборку» переложило бы работу на владельца.
#
# Одноразовый: после успешной отправки ставит метку и больше не срабатывает.
cd /opt/smart-money || exit 0
START=20260814
TODAY=$(date -u +%Y%m%d)
[ "$TODAY" -lt "$START" ] && exit 0

STAMP=output/.reminder_early_entry_sent
[ -f "$STAMP" ] && exit 0

LOG=logs/early_entry.log
mkdir -p logs
echo "=== $(date -u) замер входа по первому актору ===" >> "$LOG"
OUT=$(docker compose exec -T monitor python -m src.early_entry --telegram 2>&1)
echo "$OUT" >> "$LOG"

# метку ставим только если замер реально состоялся; при нехватке данных повторим завтра
if ! echo "$OUT" | grep -q "данных пока мало"; then
  touch "$STAMP"
  echo "метка поставлена — больше не повторяем" >> "$LOG"
else
  echo "данных мало, повторим завтра" >> "$LOG"
fi
