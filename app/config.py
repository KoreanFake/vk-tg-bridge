"""Конфигурация приложения. Секреты читаются только из окружения / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _int_list(value: str) -> list[int]:
    return [int(x.strip()) for x in value.split(",") if x.strip()]


@dataclass
class Settings:
    vk_token: str = os.getenv("VK_TOKEN", "")
    vk_group_id: int = int(os.getenv("VK_GROUP_ID", "0"))
    vk_api_version: str = os.getenv("VK_API_VERSION", "5.199")

    tg_token: str = os.getenv("TG_TOKEN", "")
    tg_group_id: int = int(os.getenv("TG_GROUP_ID", "0"))
    tg_allowed_user_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("TG_ALLOWED_USER_IDS", ""))
    )
    tg_admin_ids: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("TG_ADMIN_IDS", ""))
    )

    work_hours: str = os.getenv("WORK_HOURS", "")  # "09:00-21:00" или пусто
    reminder_intervals_min: list[int] = field(
        default_factory=lambda: _int_list(os.getenv("REMINDER_INTERVALS_MIN", "15,30"))
    )
    mask_personal_data: bool = os.getenv("MASK_PERSONAL_DATA", "false").lower() == "true"
    disable_autoclient_messages: bool = (
        os.getenv("DISABLE_AUTOCLIENT_MESSAGES", "true").lower() == "true"
    )

    db_path: str = os.getenv("DB_PATH", "bridge.db")
    log_level: str = os.getenv("LOG_LEVEL", "INFO")
    # Отображаемое название вашего сообщества/бренда (в приветствиях бота)
    brand_name: str = os.getenv("BRAND_NAME", "VK–Telegram Bridge")

    def is_allowed(self, tg_user_id: int) -> bool:
        return tg_user_id in self.tg_allowed_user_ids

    def is_admin(self, tg_user_id: int) -> bool:
        if self.tg_admin_ids:
            return tg_user_id in self.tg_admin_ids
        return tg_user_id in self.tg_allowed_user_ids

    def validate(self) -> list[str]:
        """Проверка обязательных параметров. Возвращает список ошибок."""
        errors = []
        if not self.vk_token:
            errors.append("VK_TOKEN не задан")
        if not self.vk_group_id:
            errors.append("VK_GROUP_ID не задан")
        if not self.tg_token:
            errors.append("TG_TOKEN не задан")
        if not self.tg_group_id:
            errors.append("TG_GROUP_ID не задан")
        if not self.tg_allowed_user_ids:
            errors.append("TG_ALLOWED_USER_IDS пуст — некому будет отвечать клиентам")
        return errors
