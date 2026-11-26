# Развертывание на VPS

Сервис изолирован в Docker-контейнере, **не публикует порты** (только исходящие соединения к API VK и Telegram) и не затрагивает другие сервисы сервера.

## Минимальные требования

- Linux x86_64 (проверено на Ubuntu 22.04/24.04), 1 CPU, 256 МБ RAM, 1 ГБ диска.
- Docker 24+ с Docker Compose Plugin (`docker compose version`).

## Установка

```bash
# 1. Скопируйте проект на сервер
scp -r vk-tg-bridge user@server:~/
cd ~/vk-tg-bridge

# 2. Настройте конфигурацию
cp .env.example .env
nano .env          # внесите токены и ID; .env НЕ коммитьте и не передавайте
chmod 600 .env     # доступ только владельцу

# 3. Соберите и запустите
docker compose build
docker compose up -d

# 4. Проверьте
docker compose logs --tail 50 -f bridge     # должно быть: «VK–Telegram Bridge запущен»
```

Напишите боту `/health` в группе Telegram — обе проверки должны быть ✅.

## Эксплуатация

```bash
docker compose up -d            # запуск / перезапуск
docker compose down             # остановка
docker compose logs -f bridge   # журналы (ограничены: 3 файла по 10 МБ)
docker compose build && docker compose up -d   # обновление после изменения кода
```

Автозапуск после перезагрузки VPS обеспечивает `restart: unless-stopped`.

## Резервное копирование

```bash
chmod +x scripts/*.sh
./scripts/backup.sh             # копия в data/backups/, хранение 14 дней (BACKUP_RETENTION_DAYS)
./scripts/restore.sh data/backups/bridge-20261120-120000.db   # восстановление
```

Ежедневный бэкап через cron:

```bash
crontab -e
# добавить строку:
17 3 * * * cd ~/vk-tg-bridge && ./scripts/backup.sh >> /var/log/vk-tg-backup.log 2>&1
```

## Откат

```bash
git checkout <предыдущий коммит>  # или верните архив прошлой версии
docker compose build && docker compose up -d
```

## Удаление сервиса (не трогая другие сервисы)

```bash
docker compose down
rm -rf ~/vk-tg-bridge    # включая data/ — удалит БД и бэкапы
```
