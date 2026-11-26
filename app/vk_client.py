"""Асинхронный клиент VK API: Bots Long Poll + методы сообщений и загрузка вложений.

Реализовано вручную поверх httpx, чтобы не тянуть синхронные/неподдерживаемые
библиотеки. Версия API фиксируется в конфиге.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

log = logging.getLogger("vk")

API_URL = "https://api.vk.com/method/"

# Коды ошибок VK
ERR_TOO_MANY = 6
ERR_FLOOD = 9
ERR_CAPTCHA = 14
ERR_USER_BLOCKED = 901   # пользователь запретил сообщения от сообщества
ERR_MSG_NOT_ALLOWED = 7  # нет прав на отправку этому пользователю

RETRIABLE = {ERR_TOO_MANY, ERR_FLOOD}


class VkApiError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(f"VK error {code}: {message}")
        self.code = code
        self.message = message


class VkClient:
    def __init__(self, token: str, group_id: int, api_version: str = "5.199",
                 http: Optional[httpx.AsyncClient] = None):
        self.token = token
        self.group_id = group_id
        self.v = api_version
        self.http = http or httpx.AsyncClient(timeout=40)

    async def close(self) -> None:
        await self.http.aclose()

    async def call(self, method: str, **params: Any) -> Any:
        params.update({"access_token": self.token, "v": self.v})
        for attempt in range(4):
            try:
                r = await self.http.post(API_URL + method, data=params)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                log.warning("VK %s: сетевая ошибка (%s), попытка %d", method, type(e).__name__, attempt + 1)
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if "error" in data:
                err = data["error"]
                code = err.get("error_code", -1)
                if code in RETRIABLE and attempt < 3:
                    await asyncio.sleep(2.0 * (attempt + 1))
                    continue
                raise VkApiError(code, err.get("error_msg", ""))
            return data["response"]
        raise VkApiError(-1, f"метод {method}: не удалось выполнить запрос после повторов")

    # ---- информация ----
    async def get_user(self, user_id: int) -> dict:
        resp = await self.call("users.get", user_ids=user_id)
        return resp[0] if resp else {"id": user_id, "first_name": "", "last_name": ""}

    async def health(self) -> bool:
        try:
            await self.call("groups.getById", group_id=self.group_id)
            return True
        except Exception:
            return False

    # ---- отправка ----
    async def send_message(self, peer_id: int, text: str = "",
                           attachment: str = "", random_id: int = 0) -> int:
        resp = await self.call(
            "messages.send",
            peer_id=peer_id,
            message=text[:4096],
            attachment=attachment,
            random_id=random_id,
        )
        return int(resp)

    # ---- загрузка вложений для исходящих ----
    async def upload_photo(self, peer_id: int, file_bytes: bytes, filename: str = "photo.jpg") -> str:
        server = await self.call("photos.getMessagesUploadServer", peer_id=peer_id)
        up = await self.http.post(server["upload_url"], files={"photo": (filename, file_bytes)})
        saved = await self.call(
            "photos.saveMessagesPhoto",
            server=up.json().get("server"),
            photo=up.json().get("photo"),
            hash=up.json().get("hash"),
        )
        p = saved[0]
        return f"photo{p['owner_id']}_{p['id']}"

    async def upload_doc(self, peer_id: int, file_bytes: bytes, filename: str) -> str:
        server = await self.call("docs.getMessagesUploadServer", type="doc", peer_id=peer_id)
        up = await self.http.post(server["upload_url"], files={"file": (filename, file_bytes)})
        saved = await self.call("docs.save", file=up.json().get("file"))
        d = saved.get("doc") or saved.get("audio_message") or {}
        return f"doc{d['owner_id']}_{d['id']}"

    # ---- Bots Long Poll ----
    async def get_longpoll_server(self) -> dict:
        return await self.call("groups.getLongPollServer", group_id=self.group_id)

    async def longpoll_wait(self, server: str, key: str, ts: int, wait: int = 25) -> dict:
        """Один запрос к long poll серверу. Может вернуть {"failed": N}."""
        r = await self.http.get(server, params={"act": "a_check", "key": key, "ts": ts, "wait": wait})
        return r.json()


def is_outgoing(flags: int) -> bool:
    return bool(flags & 2)


def message_attachments(msg: dict) -> list[dict]:
    """Извлекает поддерживаемые вложения из события message_new (формат LP: msg[7].attachments)."""
    return msg.get("attachments") or []
