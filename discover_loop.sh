#!/bin/bash
# Разбор победителей ПОРЦИЯМИ, устойчиво к перезапуску контейнера.
#
# Первая версия сгорела дважды. Сначала пересборка монитора уронила пять порций
# подряд за секунду: цикл не отличал «контейнер поднимается» от «работа сделана».
# Затем — кириллические имена переменных: bash допускает только ASCII, и скрипт
# падал на первой же строке. Кэш output/discover_cache.json оба раза уцелел.
cd /opt/smart-money || exit 0
LIMIT=${1:-10}
i=0
while [ $i -lt $LIMIT ]; do
  if ! docker compose ps --format '{{.Name}}' 2>/dev/null | grep -q smart-money-monitor; then
    echo "$(date -u +%H:%M:%S) контейнер не поднят — жду, итерацию не трачу"
    sleep 30
    continue
  fi
  i=$((i+1))
  echo "=== порция $i из $LIMIT · $(date -u +%H:%M:%S) ==="
  OUT=$(docker compose exec -T monitor python -m src.discover_buyers         --рост 20 --токенов 84 --ранних 25 --за-прогон 12 2>&1)
  echo "$OUT" | head -3
  if echo "$OUT" | grep -q 'разобрана полностью'; then
    echo 'ВЫБОРКА РАЗОБРАНА ПОЛНОСТЬЮ'
    echo "$OUT" | tail -20
    break
  fi
  if echo "$OUT" | grep -qE 'not running|Error response'; then
    echo 'контейнер перезапускается — итерация не засчитана'
    i=$((i-1))
    sleep 30
  fi
done
