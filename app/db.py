"""Хранилище: SQLite в режиме WAL.

Таблицы:
- conversations: соответствие VK-клиента (peer_id) и Telegram-темы, статус, метрики ответа.
- events: учет входящих/исходящих событий (идемпотентность по dedup_key).
- outbox: очередь исходящих сообщений в VK с повторными попытками.
- kv: служебные значения (например, ts long poll — переживает перезапуск).
"""
from __future__ import annotations

import time
from typing import Any, Optional

import aiosqlite

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS conversations (
    vk_peer_id      INTEGER PRIMARY KEY,
    tg_topic_id     INTEGER,
    tg_threadless_msg_id INTEGER,          -- режим без тем: id сервисного сообщения-якоря
    client_name     TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'new',   -- new|in_progress|answered|closed
    last_incoming_ts  INTEGER,             -- последнее входящее от клиента
    last_answer_ts    INTEGER,             -- последний успешный ответ клиенту
    reminder_stage    INTEGER NOT NULL DEFAULT 0,  -- сколько напоминаний отправлено
    created_ts      INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    dedup_key   TEXT PRIMARY KEY,
    direction   TEXT NOT NULL,             -- in|out
    vk_peer_id  INTEGER,
    ts          INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_peer_id  INTEGER NOT NULL,
    kind        TEXT NOT NULL,             -- text|photo|document|voice
    payload     TEXT NOT NULL,             -- JSON
    attempts    INTEGER NOT NULL DEFAULT 0,
    next_try_ts INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|failed
    last_error  TEXT,
    tg_notice_msg_id INTEGER,              -- сообщение-уведомление в теме
    created_ts  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tg_anchor (
    -- режим без тем: соответствие "сообщение бота в группе" -> клиент
    tg_message_id INTEGER PRIMARY KEY,
    vk_peer_id    INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: str):
        self.path = path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.db = await aiosqlite.connect(self.path)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None

    # ---- служебные значения ----
    async def kv_get(self, key: str) -> Optional[str]:
        cur = await self.db.execute("SELECT value FROM kv WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else None

    async def kv_set(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    # ---- идемпотентность событий ----
    async def event_seen(self, dedup_key: str) -> bool:
        cur = await self.db.execute(
            "SELECT 1 FROM events WHERE dedup_key=?", (dedup_key,)
        )
        return await cur.fetchone() is not None

    async def event_mark(self, dedup_key: str, direction: str, vk_peer_id: int) -> bool:
        """Возвращает True, если событие новое (вставлено), False — дубликат."""
        try:
            await self.db.execute(
                "INSERT INTO events(dedup_key,direction,vk_peer_id,ts) VALUES(?,?,?,?)",
                (dedup_key, direction, vk_peer_id, int(time.time())),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    # ---- диалоги ----
    async def get_conversation(self, vk_peer_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT * FROM conversations WHERE vk_peer_id=?", (vk_peer_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_conversation_by_topic(self, tg_topic_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT * FROM conversations WHERE tg_topic_id=?", (tg_topic_id,)
        )
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_conversation_by_tg_message(self, tg_message_id: int) -> Optional[dict]:
        cur = await self.db.execute(
            "SELECT vk_peer_id FROM tg_anchor WHERE tg_message_id=?", (tg_message_id,)
        )
        row = await cur.fetchone()
        if not row:
            return None
        return await self.get_conversation(row["vk_peer_id"])

    async def upsert_conversation(self, vk_peer_id: int, **fields: Any) -> dict:
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO conversations(vk_peer_id, created_ts) VALUES(?,?) "
            "ON CONFLICT(vk_peer_id) DO NOTHING",
            (vk_peer_id, now),
        )
        if fields:
            cols = ", ".join(f"{k}=?" for k in fields)
            await self.db.execute(
                f"UPDATE conversations SET {cols} WHERE vk_peer_id=?",
                (*fields.values(), vk_peer_id),
            )
        await self.db.commit()
        return await self.get_conversation(vk_peer_id)

    async def set_status(self, vk_peer_id: int, status: str) -> None:
        await self.upsert_conversation(vk_peer_id, status=status)

    async def add_anchor(self, tg_message_id: int, vk_peer_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO tg_anchor(tg_message_id, vk_peer_id) VALUES(?,?)",
            (tg_message_id, vk_peer_id),
        )
        await self.db.commit()

    async def unanswered(self, now: int) -> list[dict]:
        """Диалоги с неотвеченным входящим сообщением (для напоминаний)."""
        cur = await self.db.execute(
            """
            SELECT * FROM conversations
            WHERE status != 'closed'
              AND last_incoming_ts IS NOT NULL
              AND (last_answer_ts IS NULL OR last_answer_ts < last_incoming_ts)
            """,
        )
        return [dict(r) for r in await cur.fetchall()]

    async def stats(self) -> dict:
        cur = await self.db.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(status='new') AS new,
                   SUM(status='in_progress') AS in_progress,
                   SUM(status='answered') AS answered,
                   SUM(status='closed') AS closed
            FROM conversations
            """
        )
        row = dict(await cur.fetchone())
        cur2 = await self.db.execute(
            "SELECT COUNT(*) AS c FROM outbox WHERE status='pending'"
        )
        row["outbox_pending"] = (await cur2.fetchone())["c"]
        return {k: (v or 0) for k, v in row.items()}

    async def delete_conversation(self, vk_peer_id: int) -> None:
        """Удаление данных клиента (право на забвение / правила хранения)."""
        await self.db.execute("DELETE FROM conversations WHERE vk_peer_id=?", (vk_peer_id,))
        await self.db.execute("DELETE FROM events WHERE vk_peer_id=?", (vk_peer_id,))
        await self.db.execute("DELETE FROM outbox WHERE vk_peer_id=?", (vk_peer_id,))
        await self.db.execute("DELETE FROM tg_anchor WHERE vk_peer_id=?", (vk_peer_id,))
        await self.db.commit()

    # ---- очередь исходящих ----
    async def outbox_add(self, vk_peer_id: int, kind: str, payload: str,
                         tg_notice_msg_id: Optional[int] = None) -> int:
        cur = await self.db.execute(
            "INSERT INTO outbox(vk_peer_id,kind,payload,next_try_ts,tg_notice_msg_id,created_ts)"
            " VALUES(?,?,?,?,?,?)",
            (vk_peer_id, kind, payload, int(time.time()), tg_notice_msg_id, int(time.time())),
        )
        await self.db.commit()
        return cur.lastrowid

    async def outbox_due(self, now: int) -> list[dict]:
        cur = await self.db.execute(
            "SELECT * FROM outbox WHERE status='pending' AND next_try_ts<=? ORDER BY id",
            (now,),
        )
        return [dict(r) for r in await cur.fetchall()]

    async def outbox_update(self, outbox_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k}=?" for k in fields)
        await self.db.execute(
            f"UPDATE outbox SET {cols} WHERE id=?", (*fields.values(), outbox_id)
        )
        await self.db.commit()
