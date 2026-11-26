"""Общие типы данных маршрутизатора (независимы от aiogram и VK SDK)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Attachment:
    kind: str            # photo|doc|audio_message|other
    url: str = ""        # прямая ссылка на файл (VK)
    title: str = ""
    file_bytes: Optional[bytes] = None   # для исходящих из Telegram
    filename: str = ""


@dataclass
class VkIncoming:
    """Нормализованное событие сообщения VK (входящее или эхо исходящего)."""
    peer_id: int
    message_id: int
    conversation_message_id: int
    date: int
    from_id: int
    text: str
    attachments: list[Attachment] = field(default_factory=list)
    outgoing: bool = False

    @property
    def dedup_key(self) -> str:
        return f"vk:{self.peer_id}:{self.conversation_message_id}"


@dataclass
class StaffMessage:
    """Сообщение сотрудника из Telegram, предназначенное клиенту."""
    tg_user_id: int
    tg_message_id: int
    text: str = ""
    photos: list[Attachment] = field(default_factory=list)
    document: Optional[Attachment] = None
    voice: Optional[Attachment] = None


STATUS_TITLES = {
    "new": "Новый",
    "in_progress": "В работе",
    "answered": "Отвечено",
    "closed": "Закрыто",
}

STATUS_EMOJI = {
    "new": "🆕",
    "in_progress": "🔄",
    "answered": "✅",
    "closed": "📁",
}
