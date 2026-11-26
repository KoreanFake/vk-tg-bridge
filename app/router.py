"""Маршрутизатор: связывает VK и Telegram.

Работает через интерфейс-шлюз Telegram (любая реализация с методами
ensure_thread / deliver_client_message / notify_staff / set_status_view /
send_reminder), что позволяет тестировать логику без Telegram.
"""
from __future__ import annotations

import json
import logging
import random
import time
from typing import Optional

from .db import Database
from .models import Attachment, STATUS_TITLES, StaffMessage, VkIncoming
from .utils import in_work_hours, mask_personal_data
from .vk_client import ERR_USER_BLOCKED, VkApiError, VkClient

log = logging.getLogger("router")

MAX_RETRIES = 5


class Router:
    def __init__(self, db: Database, vk: VkClient, tg, settings):
        self.db = db
        self.vk = vk
        self.tg = tg  # шлюз Telegram (TgBridge или тестовый двойник)
        self.settings = settings

    # ================= VK -> Telegram =================

    async def handle_vk_incoming(self, msg: VkIncoming) -> None:
        """Входящее сообщение клиента из VK."""
        if not await self.db.event_mark(msg.dedup_key, "in", msg.peer_id):
            log.info("дубликат события VK %s — пропуск", msg.dedup_key)
            return

        conv = await self.db.get_conversation(msg.peer_id)
        if conv is None:
            user = await self._safe_get_user(msg.from_id or msg.peer_id)
            name = f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or f"id{msg.peer_id}"
            conv = await self.db.upsert_conversation(msg.peer_id, client_name=name)

        # Создать тему/якорь, если ещё нет (устойчиво к перезапуску: читаем из БД)
        if not conv.get("tg_topic_id") and not conv.get("tg_threadless_msg_id"):
            conv = await self.tg.ensure_thread(conv)

        # Новое обращение после закрытия — возвращаем в активное состояние
        if conv["status"] == "closed":
            await self.db.set_status(msg.peer_id, "new")
            await self.tg.set_status_view(conv, "new")
            await self.tg.notify_staff(conv, "🔔 Клиент написал снова — диалог возобновлён (статус: Новый).")
            conv = await self.db.get_conversation(msg.peer_id)

        await self.db.upsert_conversation(
            msg.peer_id, last_incoming_ts=int(msg.date), reminder_stage=0
        )

        text = msg.text
        if self.settings.mask_personal_data and text:
            text = mask_personal_data(text)

        await self.tg.deliver_client_message(conv, msg, text)

    async def handle_vk_outgoing_echo(self, msg: VkIncoming) -> None:
        """Эхо исходящего сообщения VK (сотрудник ответил прямо из интерфейса ВК)."""
        if not await self.db.event_mark(msg.dedup_key + ":echo", "out", msg.peer_id):
            return
        conv = await self.db.get_conversation(msg.peer_id)
        if not conv or (not conv.get("tg_topic_id") and not conv.get("tg_threadless_msg_id")):
            return
        await self.db.upsert_conversation(
            msg.peer_id, last_answer_ts=int(msg.date), reminder_stage=0, status="answered"
        )
        await self.tg.deliver_outgoing_echo(conv, msg)
        await self.tg.set_status_view(conv, "answered")

    async def _safe_get_user(self, user_id: int) -> dict:
        try:
            return await self.vk.get_user(user_id)
        except Exception as e:
            log.warning("не удалось получить профиль VK %s: %s", user_id, type(e).__name__)
            return {"first_name": "", "last_name": ""}

    # ================= Telegram -> VK =================

    async def handle_staff_message(self, conv: dict, staff: StaffMessage) -> None:
        """Ответ сотрудника: ставим в очередь с идемпотентным random_id."""
        # Идемпотентность: одно TG-сообщение -> один random_id, повтор не отправит дубль
        if not await self.db.event_mark(f"tg:{staff.tg_message_id}", "out", conv["vk_peer_id"]):
            await self.tg.notify_staff(conv, "⚠️ Это сообщение уже было отправлено клиенту ранее.")
            return

        payload = {
            "text": staff.text,
            "photos": [a.file_bytes.hex() for a in staff.photos if a.file_bytes],
            "doc": staff.document.file_bytes.hex() if staff.document and staff.document.file_bytes else None,
            "doc_name": staff.document.filename if staff.document else None,
            "voice": staff.voice.file_bytes.hex() if staff.voice and staff.voice.file_bytes else None,
            "voice_name": staff.voice.filename if staff.voice else None,
            "random_id": staff.tg_message_id & 0x7FFFFFFF,
        }
        outbox_id = await self.db.outbox_add(conv["vk_peer_id"], "reply", json.dumps(payload))
        log.info("ответ для VK %s поставлен в очередь #%s", conv["vk_peer_id"], outbox_id)
        await self.process_outbox_item(await self._outbox_get(outbox_id))

    async def _outbox_get(self, outbox_id: int) -> dict:
        cur = await self.db.db.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,))
        row = await cur.fetchone()
        return dict(row)

    async def outbox_tick(self, now: Optional[int] = None) -> None:
        now = now or int(time.time())
        for item in await self.db.outbox_due(now):
            await self.process_outbox_item(item)

    async def process_outbox_item(self, item: dict) -> None:
        conv = await self.db.get_conversation(item["vk_peer_id"])
        payload = json.loads(item["payload"])
        try:
            attachments = []
            for hexbytes in payload.get("photos") or []:
                attachments.append(await self.vk.upload_photo(item["vk_peer_id"], bytes.fromhex(hexbytes)))
            if payload.get("doc"):
                attachments.append(await self.vk.upload_doc(
                    item["vk_peer_id"], bytes.fromhex(payload["doc"]), payload.get("doc_name") or "document"))
            if payload.get("voice"):
                # Голосовые пересылаем как документ (совместимо с messages API)
                attachments.append(await self.vk.upload_doc(
                    item["vk_peer_id"], bytes.fromhex(payload["voice"]), payload.get("voice_name") or "voice.ogg"))

            msg_id = await self.vk.send_message(
                peer_id=item["vk_peer_id"],
                text=payload.get("text") or "",
                attachment=",".join(attachments),
                random_id=payload.get("random_id") or random.getrandbits(31),
            )
            await self.db.outbox_update(item["id"], status="sent", last_error=None)
            await self.db.upsert_conversation(
                item["vk_peer_id"], last_answer_ts=int(time.time()), status="answered"
            )
            await self.db.event_mark(f"vk:sent:{msg_id}", "out", item["vk_peer_id"])
            if conv:
                await self.tg.notify_staff(conv, "✅ Ответ доставлен клиенту во ВКонтакте.")
                await self.tg.set_status_view(conv, "answered")
            log.info("ответ #%s отправлен клиенту VK %s (msg %s)", item["id"], item["vk_peer_id"], msg_id)

        except VkApiError as e:
            if e.code == ERR_USER_BLOCKED:
                await self.db.outbox_update(item["id"], status="failed", last_error="user_blocked")
                if conv:
                    await self.tg.notify_staff(
                        conv, "⛔️ Не доставлено: клиент запретил сообщения от сообщества.")
                log.warning("клиент VK %s запретил сообщения", item["vk_peer_id"])
                return
            await self._retry_or_fail(item, f"VK {e.code}")

        except Exception as e:  # сеть, Telegram недоступен и т.п.
            await self._retry_or_fail(item, type(e).__name__)

    async def _retry_or_fail(self, item: dict, error: str) -> None:
        attempts = item["attempts"] + 1
        if attempts >= MAX_RETRIES:
            await self.db.outbox_update(item["id"], status="failed", attempts=attempts, last_error=error)
            conv = await self.db.get_conversation(item["vk_peer_id"])
            if conv:
                await self.tg.notify_staff(
                    conv, f"❌ Ответ НЕ доставлен клиенту (ошибка: {error}). "
                          f"Попытки исчерпаны — свяжитесь с клиентом иначе.")
            log.error("очередь #%s: ответ не доставлен (%s)", item["id"], error)
        else:
            delay = min(60 * 2 ** attempts, 1800)
            await self.db.outbox_update(
                item["id"], attempts=attempts, next_try_ts=int(time.time()) + delay, last_error=error)
            log.info("очередь #%s: повтор через %s сек (%s)", item["id"], delay, error)

    # ================= напоминания =================

    async def reminders_tick(self, now: Optional[int] = None) -> None:
        now = now or int(time.time())
        if not in_work_hours(self.settings.work_hours):
            return
        intervals = sorted(self.settings.reminder_intervals_min)
        for conv in await self.db.unanswered(now):
            elapsed_min = (now - conv["last_incoming_ts"]) / 60
            stage = conv["reminder_stage"]
            while stage < len(intervals) and elapsed_min >= intervals[stage]:
                stage += 1
            if stage > conv["reminder_stage"]:
                await self.db.upsert_conversation(conv["vk_peer_id"], reminder_stage=stage)
                await self.tg.send_reminder(conv, stage, int(elapsed_min))
                log.info("напоминание #%s по клиенту VK %s", stage, conv["vk_peer_id"])
