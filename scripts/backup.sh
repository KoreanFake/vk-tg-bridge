#!/usr/bin/env bash
# Резервная копия базы соответствий диалогов (SQLite, онлайн-бэкап без остановки).
# Запуск: ./scripts/backup.sh   (рекомендуется ежедневно через cron)
set -euo pipefail

DATA_DIR="$(dirname "$0")/../data"
BACKUP_DIR="$DATA_DIR/backups"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
cp "$DATA_DIR/bridge.db" "$BACKUP_DIR/bridge-$TS.db"

# Удаляем копии старше срока хранения
find "$BACKUP_DIR" -name 'bridge-*.db' -mtime "+$RETENTION_DAYS" -delete
echo "Бэкап создан: $BACKUP_DIR/bridge-$TS.db"
