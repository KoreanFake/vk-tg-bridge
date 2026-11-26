"""Цикл Bots Long Poll ВКонтакте.

- ts сохраняется в БД -> события не теряются и не дублируются при перезапуске.
- failed=1 -> обновить ts; failed=2/3 -> получить новый key; сеть -> backoff.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .models import Attachment, VkIncoming
from .vk_client import VkClient, is_outgoing

log = logging.getLogger("vk.poller")

MAX_ATTACH_PHOTO_SIZES = "wzyxrqponml"


def _extract_attachments(msg: dict) -> list[Attachment]:
    result = []
    for att in msg.get("attachments") or []:
        kind = att.get("type", "other")
        body = att.get(kind) or {}
        if kind == "photo":
            sizes = body.get("sizes") or []
            url = ""
            for key in MAX_ATTACH_PHOTO_SIZES:
                url = next((s["url"] for s in sizes if s["type"] == key), "")
                if url:
                    break
            result.append(Attachment(kind="photo", url=url))
        elif kind == "doc":
            result.append(Attachment(
                kind="doc", url=body.get("url", ""), title=body.get("title", "Документ")))
        elif kind == "audio_message":
            result.append(Attachment(
                kind="audio_message", url=body.get("link_ogg") or body.get("link_mp3", ""),
                title="Голосовое сообщение"))
        elif kind == "video":
            owner_id, vid = body.get("owner_id"), body.get("id")
            url = f"https://vk.ru/video{owner_id}_{vid}" if owner_id and vid else ""
            result.append(Attachment(kind="video", url=url, title=body.get("title", "Видео")))
        else:
            result.append(Attachment(kind=kind, title=body.get("title", "")))
    return result


def parse_message_event(update: dict) -> Optional[VkIncoming]:
    """Событие type=4 (новое сообщение) в формате Bots Long Poll."""
    if update.get("type") != "message_new" and update.get("type") != 4:
        return None
    obj = update.get("object")
    if obj is None:
        return None
    if isinstance(obj, list):
        # классический числовой формат LP: [id, flags, peer_id, ts, text, ...]
        msg_id, flags, peer_id, ts, text = obj[0], obj[1], obj[2], obj[3], obj[4]
        extra = obj[7] if len(obj) > 7 and isinstance(obj[7], dict) else {}
        return VkIncoming(
            peer_id=peer_id, message_id=msg_id,
            conversation_message_id=extra.get("conversation_message_id", msg_id),
            date=ts, from_id=int(extra.get("from", peer_id)), text=text,
            attachments=_extract_attachments(extra),
            outgoing=is_outgoing(flags))
    msg = obj.get("message") or obj  # в новых версиях событие содержит message в object
    from_id = msg.get("from_id", msg.get("peer_id"))
    return VkIncoming(
        peer_id=msg["peer_id"], message_id=msg["id"],
        conversation_message_id=msg.get("conversation_message_id", msg["id"]),
        date=msg.get("date", 0), from_id=from_id, text=msg.get("text", ""),
        attachments=_extract_attachments(msg),
        outgoing=from_id < 0)  # от сообщества -> исходящее


class VkPoller:
    def __init__(self, vk: VkClient, db, router):
        self.vk = vk
        self.db = db
        self.router = router
        self._stop = asyncio.Event()

    async def run(self) -> None:
        backoff = 1
        server = key = None
        ts = int(await self.db.kv_get("vk_ts") or 0)
        while not self._stop.is_set():
            try:
                if server is None:
                    lp = await self.vk.get_longpoll_server()
                    server, key = lp["server"], lp["key"]
                    if not ts:
                        ts = int(lp["ts"])  # впервые: начинаем с текущего момента
                        await self.db.kv_set("vk_ts", str(ts))
                resp = await self.vk.longpoll_wait(server, key, ts)
                if "failed" in resp:
                    code = resp["failed"]
                    if code == 1:
                        ts = int(resp["ts"])
                    elif code in (2, 3):
                        server = None  # новый key
                    else:
                        lp = await self.vk.get_longpoll_server()
                        server, key, ts = lp["server"], lp["key"], int(lp["ts"])
                    continue
                backoff = 1
                ts = int(resp["ts"])
                await self.db.kv_set("vk_ts", str(ts))
                for upd in resp.get("updates", []):
                    try:
                        await self._dispatch(upd)
                    except Exception:
                        log.exception("ошибка обработки события VK")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("long poll: %s, повтор через %s сек", type(e).__name__, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _dispatch(self, upd: dict) -> None:
        msg = parse_message_event(upd)
        if not msg or msg.peer_id >= 2_000_000_000:  # игнорируем беседы, только личные
            return
        if msg.outgoing:
            await self.router.handle_vk_outgoing_echo(msg)
        else:
            await self.router.handle_vk_incoming(msg)

    def stop(self) -> None:
        self._stop.set()
