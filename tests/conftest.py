"""Тестовые двойники VK и Telegram + фабрики."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest_asyncio

from app.config import Settings
from app.db import Database
from app.models import VkIncoming


class FakeVk:
    """Имитация VK API. send_fail_code задаёт ошибку отправки; sent — журнал отправок."""

    def __init__(self):
        self.sent: list[dict] = []
        self.send_fail_code = None
        self.users = {}
        self._msg_id = 1000
        self.healthy = True

    async def get_user(self, user_id: int) -> dict:
        return self.users.get(user_id, {"id": user_id, "first_name": "Иван", "last_name": "Тестов"})

    async def health(self) -> bool:
        return self.healthy

    async def send_message(self, peer_id, text="", attachment="", random_id=0):
        if self.send_fail_code is not None:
            from app.vk_client import VkApiError
            raise VkApiError(self.send_fail_code, "test error")
        self._msg_id += 1
        self.sent.append({"peer_id": peer_id, "text": text, "attachment": attachment,
                          "random_id": random_id, "msg_id": self._msg_id})
        return self._msg_id

    async def upload_photo(self, peer_id, file_bytes, filename="photo.jpg"):
        return f"photo-1_{len(self.sent)}"

    async def upload_doc(self, peer_id, file_bytes, filename="doc"):
        return "doc-1_77"


class FakeTg:
    """Имитация Telegram-шлюза: записывает вызовы, выдаёт фиктивные темы."""

    def __init__(self, db):
        self.db = db
        self.delivered: list[tuple[int, str]] = []
        self.notices: list[tuple[int, str]] = []
        self.reminders: list[tuple[int, int]] = []
        self.statuses: list[tuple[int, str]] = []
        self.echoes: list[tuple[int, str]] = []
        self._topic_seq = 100

    async def ensure_thread(self, conv):
        self._topic_seq += 1
        return await self.db.upsert_conversation(conv["vk_peer_id"], tg_topic_id=self._topic_seq)

    async def deliver_client_message(self, conv, msg, text):
        self.delivered.append((conv["vk_peer_id"], text))

    async def deliver_outgoing_echo(self, conv, msg):
        self.echoes.append((conv["vk_peer_id"], msg.text))

    async def notify_staff(self, conv, text):
        self.notices.append((conv["vk_peer_id"], text))

    async def set_status_view(self, conv, status):
        await self.db.set_status(conv["vk_peer_id"], status)
        self.statuses.append((conv["vk_peer_id"], status))

    async def send_reminder(self, conv, stage, elapsed_min):
        self.reminders.append((conv["vk_peer_id"], stage))


@pytest_asyncio.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.connect()
    settings = Settings()
    settings.work_hours = ""            # круглосуточно
    settings.reminder_intervals_min = [15, 30]
    settings.mask_personal_data = False
    vk = FakeVk()
    tg = FakeTg(db)
    from app.router import Router
    router = Router(db, vk, tg, settings)
    yield db, vk, tg, router, settings
    await db.close()


def vk_msg(peer_id=555, cmi=1, text="Здравствуйте!", date=1_700_000_000,
           outgoing=False, from_id=None) -> VkIncoming:
    return VkIncoming(
        peer_id=peer_id, message_id=cmi, conversation_message_id=cmi,
        date=date, from_id=from_id or (-1 if outgoing else peer_id),
        text=text, outgoing=outgoing)
