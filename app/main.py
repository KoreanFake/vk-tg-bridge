"""Точка входа: собирает модули и запускает циклы опроса."""
from __future__ import annotations

import asyncio
import logging
import sys

from .config import Settings
from .db import Database
from .logsetup import setup_logging
from .router import Router
from .tg_bridge import TgBridge
from .vk_client import VkClient
from .vk_poller import VkPoller

log = logging.getLogger("main")


async def amain() -> None:
    settings = Settings()
    setup_logging(settings.log_level)
    errors = settings.validate()
    if errors:
        for e in errors:
            log.error("конфигурация: %s", e)
        sys.exit("Заполните .env (см. .env.example) и перезапустите.")

    db = Database(settings.db_path)
    await db.connect()

    vk = VkClient(settings.vk_token, settings.vk_group_id, settings.vk_api_version)
    tg = TgBridge(settings, db)
    router = Router(db, vk, tg, settings)
    tg.router_holder["router"] = router
    poller = VkPoller(vk, db, router)

    async def periodic(fn, interval: int):
        while True:
            await asyncio.sleep(interval)
            try:
                await fn()
            except Exception:
                log.exception("фоновая задача завершилась с ошибкой")

    tasks = [
        asyncio.create_task(poller.run()),
        asyncio.create_task(tg.run_polling()),
        asyncio.create_task(periodic(router.outbox_tick, 10)),
        asyncio.create_task(periodic(router.reminders_tick, 60)),
    ]
    log.info("VK–Telegram Bridge запущен")
    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        poller.stop()
        await vk.close()
        await tg.close()
        await db.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
