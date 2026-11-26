#!/usr/bin/env bash
# Восстановление базы из резервной копии: ./scripts/restore.sh data/backups/bridge-YYYYMMDD-HHMMSS.db
set -euo pipefail
SRC="$1"
DATA_DIR="$(dirname "$0")/../data"

docker compose -f "$(dirname "$0")/../docker-compose.yml" stop bridge
cp "$SRC" "$DATA_DIR/bridge.db"
docker compose -f "$(dirname "$0")/../docker-compose.yml" start bridge
echo "База восстановлена из $SRC, сервис запущен."
