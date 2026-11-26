#!/usr/bin/env bash
# Интерактивная установка VK→Telegram Bridge на чистый VPS (Ubuntu/Debian).
# Запуск: bash scripts/setup.sh
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== VK→Telegram Bridge: установка ==="
echo

# --- проверка Docker ---
if ! command -v docker >/dev/null; then
  echo "Docker не найден. Устанавливаю..."
  apt update -qq && apt install -y -qq docker.io
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Устанавливаю плагин Docker Compose..."
  apt update -qq && apt install -y -qq docker-compose-v2
fi
echo "Docker: $(docker --version)"

# --- конфиг ---
if [ -f .env ]; then
  echo ".env уже существует — пропускаю опрос. Для перенастройки отредактируйте .env вручную."
else
  echo
  echo "Заполните параметры (подсказки — в docs/). Ничего не отображается при вводе — это нормально для токенов."
  read -rp "1) Токен сообщества VK (Управление → Работа с API → Ключи доступа): " VK_TOKEN
  read -rp "2) Числовой ID сообщества VK (например 211249130): " VK_GROUP_ID
  read -rp "3) Токен Telegram-бота от @BotFather: " TG_TOKEN
  read -rp "4) ID Telegram-группы (например -1001234567890): " TG_GROUP_ID
  read -rp "5) Telegram ID сотрудников через запятую (узнать: @userinfobot): " IDS
  read -rp "6) Название бренда для приветствия [VK–Telegram Bridge]: " BRAND
  BRAND="${BRAND:-VK–Telegram Bridge}"
  cat > .env <<EOF
VK_TOKEN=$VK_TOKEN
VK_GROUP_ID=$VK_GROUP_ID
VK_API_VERSION=5.199
TG_TOKEN=$TG_TOKEN
TG_GROUP_ID=$TG_GROUP_ID
TG_ALLOWED_USER_IDS=$IDS
TG_ADMIN_IDS=$IDS
WORK_HOURS=09:00-21:00
REMINDER_INTERVALS_MIN=15,30
MASK_PERSONAL_DATA=false
DISABLE_AUTOCLIENT_MESSAGES=true
DB_PATH=/data/bridge.db
LOG_LEVEL=INFO
BRAND_NAME=$BRAND
EOF
  chmod 600 .env
  echo ".env создан (права 600)."
fi

# --- запуск ---
mkdir -p data && chown -R 1000:1000 data
docker compose build
docker compose up -d
sleep 3
docker compose logs --tail 10 bridge

echo
echo "=== Готово ==="
echo "Напишите боту /health в Telegram-группе — обе проверки должны быть ✅."
echo "Логи:  docker compose logs -f bridge"
echo "Бэкап: настройте по docs/deploy-vps.md (раздел «Резервное копирование»)"
