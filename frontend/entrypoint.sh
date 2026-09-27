#!/bin/sh
# Скрипт, который nginx-образ запускает сам через /docker-entrypoint.d/.
# Подменяет плейсхолдеры (__MODE__, __API_BASE__, __WS_URL__) в config.js
# на значения переменных окружения. Отсутствующая переменная → пустое поле,
# и config.js применит fallback-константу.
set -eu

SRC="/usr/share/nginx/html/js/config.js"
# Пишем подставленную копию отдельно, а не правим config.js на месте: тогда папку frontend можно
# примонтировать в контейнер только для чтения (локальная разработка, docker-compose.override.yml),
# и файл в репозитории не меняется. nginx отдаёт /js/config.js из OUT (см. nginx.conf).
OUT_DIR="/usr/share/nginx/runtime"
OUT="$OUT_DIR/config.js"
mkdir -p "$OUT_DIR"

MODE="${MODE:-live}"
API_BASE="${API_BASE:-http://localhost:8000}"
WS_URL="${WS_URL:-ws://localhost:8000/ws}"

sed \
  -e "s|__MODE__|${MODE}|g" \
  -e "s|__API_BASE__|${API_BASE}|g" \
  -e "s|__WS_URL__|${WS_URL}|g" \
  "$SRC" > "$OUT"

echo "[frontend] config.js patched: MODE=${MODE} API_BASE=${API_BASE} WS_URL=${WS_URL}"
