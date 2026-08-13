#!/bin/bash
# Разбор победителей ПОРЦИЯМИ, устойчиво к перезапуску контейнера.
#
# Три раза сломался, прежде чем заработал, и каждый раз по-своему:
#   1. пересборка монитора уронила пять порций подряд за секунду — цикл не отличал
#      «контейнер поднимается» от «работа сделана»;
#   2. кириллические имена переменных — bash допускает в идентификаторах только ASCII;
#   3. запись через heredoc из ssh побила строку — поэтому файл теперь лежит в
#      репозитории и копируется как файл, а не собирается на месте.
# Кэш output/discover_cache.json уцелел во всех трёх случаях: он пишется после
# каждого токена, и работа не теряется.
cd /opt/smart-money || exit 0
LIMIT=${1:-10}
i=0
while [ "$i" -lt "$LIMIT" ]; do
  if ! docker compose ps --format '{{.Name}}' 2>/dev/null | grep -q smart-money-monitor; then
    echo "$(date -u +%H:%M:%S) контейнер не поднят — жду, итерацию не трачу"
    sleep 30
    continue
  fi
  i=$((i + 1))
  echo "=== порция $i из $LIMIT · $(date -u +%H:%M:%S) ==="
  OUT=$(docker compose exec -T monitor python -m src.discover_buyers \
        --рост 20 --токенов 84 --ранних 25 --за-прогон 12 2>&1)
  echo "$OUT" | head -3
  if echo "$OUT" | grep -q 'разобрана полностью'; then
    echo "ВЫБОРКА РАЗОБРАНА ПОЛНОСТЬЮ"
    echo "$OUT"
    break
  fi
  if echo "$OUT" | grep -qE 'not running|Error response'; then
    echo "контейнер перезапускается — итерация не засчитана"
    i=$((i - 1))
    sleep 30
  fi
done
