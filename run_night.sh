#!/bin/bash
# Сборщик + paper на ночь в фоне. Остановить: ./run_night.sh stop. Утром: paper_summary.md (обновляется каждый час).
cd "$(dirname "$0")"
if [ "$1" = "stop" ]; then
  for f in .collector.pid .paper.pid .caffeinate.pid; do [ -f $f ] && kill "$(cat $f)" 2>/dev/null; rm -f $f; done
  python3 paper.py --summary; echo "остановлено; сводка в paper_summary.md"; exit 0
fi
[ -f dragon_core.py ] || { echo "нет dragon_core.py в $(pwd) — положи файл ядра сюда"; exit 1; }
python3 -c "import pandas" 2>/dev/null || { echo "нужен pandas: pip3 install pandas"; exit 1; }
nohup python3 hl_collector.py >/dev/null 2>&1 & echo $! > .collector.pid
nohup python3 paper.py >/dev/null 2>&1 & echo $! > .paper.pid
command -v caffeinate >/dev/null && { caffeinate -dimsu -w "$(cat .collector.pid)" & echo $! > .caffeinate.pid; }
echo "запущено: сборщик $(cat .collector.pid), paper $(cat .paper.pid). Логи: hl_collector.log, paper.log"
