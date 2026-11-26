"""Telegram-сторона: aiogram-бот, темы супергруппы, команды, шлюз для роутера.

Режимы:
- Темы (форум): для каждого клиента VK создаётся отдельная тема.
- Запасной режим без тем: якорные сообщения + ответ через reply (защита от ошибки адресата).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from aiogram import Bot, Dispatcher, F, Router as AiogramRouter
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, URLInputFile,
)

from .db import Database
from .logsetup import clip
from .models import STATUS_EMOJI, STATUS_TITLES, Attachment, StaffMessage, VkIncoming

log = logging.getLogger("tg")

SERVICE_PREFIXES = ("/", "!")


def status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 В работе", callback_data="st:in_progress"),
        InlineKeyboardButton(text="✅ Отвечено", callback_data="st:answered"),
        InlineKeyboardButton(text="📁 Закрыто", callback_data="st:closed"),
    ]])


class TgBridge:
    def __init__(self, settings, db: Database):
        self.settings = settings
        self.db = db
        self.bot = Bot(settings.tg_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher()
        self.router_holder: dict = {}  # сюда main кладёт Router (обратная связь)
        self.forum_enabled: Optional[bool] = None
        self._register_handlers()

    # ---------- авторизация ----------
    def _allowed(self, user_id: int) -> bool:
        return self.settings.is_allowed(user_id)

    def _admin(self, user_id: int) -> bool:
        return self.settings.is_admin(user_id)

    # ---------- шлюз для роутера ----------

    async def ensure_thread(self, conv: dict) -> dict:
        """Создаёт тему (или якорь) для клиента. Идемпотентно: повтор не создаёт дубль."""
        conv = await self.db.get_conversation(conv["vk_peer_id"])  # перечитать
        if conv.get("tg_topic_id") or conv.get("tg_threadless_msg_id"):
            return conv
        title = f"{STATUS_EMOJI['new']} {conv['client_name']}"
        if self.forum_enabled is not False:
            try:
                topic = await self.bot.create_forum_topic(
                    chat_id=self.settings.tg_group_id, name=title[:128])
                self.forum_enabled = True
                conv = await self.db.upsert_conversation(conv["vk_peer_id"], tg_topic_id=topic.message_thread_id)
                await self._send_topic_header(conv)
                return conv
            except TelegramBadRequest as e:
                log.warning("темы недоступны (%s) — запасной режим без тем", clip(str(e)))
                self.forum_enabled = False
        # Запасной режим: якорное сообщение
        anchor = await self.bot.send_message(
            self.settings.tg_group_id,
            f"👤 <b>{conv['client_name']}</b>\n"
            f"VK: https://vk.com/id{conv['vk_peer_id']}\n"
            f"Статус: {STATUS_TITLES['new']}\n"
            f"<i>Отвечайте клиенту через reply на сообщения этого диалога.</i>",
            reply_markup=status_keyboard(),
        )
        await self.db.add_anchor(anchor.message_id, conv["vk_peer_id"])
        conv = await self.db.upsert_conversation(
            conv["vk_peer_id"], tg_threadless_msg_id=anchor.message_id)
        return conv

    async def _send_topic_header(self, conv: dict) -> None:
        msg = await self.bot.send_message(
            self.settings.tg_group_id,
            f"👤 <b>{conv['client_name']}</b>\n"
            f"Профиль VK: https://vk.com/id{conv['vk_peer_id']}\n"
            f"Статус: {STATUS_TITLES[conv['status']]}\n\n"
            f"Пишите ответ прямо в эту тему — он уйдёт клиенту во ВКонтакте.\n"
            f"Служебные сообщения, начинающиеся с «/» или «!», клиенту не отправляются.",
            message_thread_id=conv["tg_topic_id"],
            reply_markup=status_keyboard(),
        )
        await self.bot.pin_chat_message(self.settings.tg_group_id, msg.message_id, disable_notification=True)

    def _thread_kwargs(self, conv: dict) -> dict:
        return {"message_thread_id": conv["tg_topic_id"]} if conv.get("tg_topic_id") else {}

    async def deliver_client_message(self, conv: dict, msg: VkIncoming, text: str) -> None:
        """Показывает сотрудникам входящее сообщение клиента."""
        when = time.strftime("%d.%m %H:%M", time.localtime(msg.date))
        header = f"💬 <b>Клиент</b> · {when}\n"
        sent_anchor = None
        if text:
            m = await self.bot.send_message(
                self.settings.tg_group_id, header + text, **self._thread_kwargs(conv))
            sent_anchor = m
        for att in msg.attachments:
            try:
                if att.kind == "photo" and att.url:
                    m = await self.bot.send_photo(
                        self.settings.tg_group_id, URLInputFile(att.url),
                        caption=header if not text else None, **self._thread_kwargs(conv))
                    sent_anchor = sent_anchor or m
                elif att.kind in ("doc", "audio_message") and att.url:
                    m = await self.bot.send_document(
                        self.settings.tg_group_id, URLInputFile(att.url),
                        caption=header + (att.title or "Вложение") if not text else att.title or "Вложение",
                        **self._thread_kwargs(conv))
                    sent_anchor = sent_anchor or m
                else:
                    body = (f"🎬 <a href=\"{att.url}\">{att.title or 'Видео'}</a> — откройте ссылку во ВКонтакте"
                            if att.kind == "video" and att.url
                            else f"[вложение типа «{att.kind}» недоступно для пересылки]")
                    m = await self.bot.send_message(
                        self.settings.tg_group_id, header + body,
                        **self._thread_kwargs(conv))
                    sent_anchor = sent_anchor or m
            except Exception as e:
                log.warning("не удалось переслать вложение клиента: %s", clip(str(e)))
        if not text and not msg.attachments:
            m = await self.bot.send_message(
                self.settings.tg_group_id, header + "[пустое сообщение]",
                **self._thread_kwargs(conv))
            sent_anchor = m
        if sent_anchor and not conv.get("tg_topic_id"):
            await self.db.add_anchor(sent_anchor.message_id, conv["vk_peer_id"])

    async def deliver_outgoing_echo(self, conv: dict, msg: VkIncoming) -> None:
        when = time.strftime("%d.%m %H:%M", time.localtime(msg.date))
        await self.bot.send_message(
            self.settings.tg_group_id,
            f"🟣 <b>Ответ из интерфейса ВК</b> · {when}\n{msg.text or '[вложение]'}",
            **self._thread_kwargs(conv))

    async def notify_staff(self, conv: dict, text: str) -> None:
        await self.bot.send_message(self.settings.tg_group_id, text, **self._thread_kwargs(conv))

    async def set_status_view(self, conv: dict, status: str) -> None:
        await self.db.set_status(conv["vk_peer_id"], status)
        if conv.get("tg_topic_id"):
            try:
                await self.bot.edit_forum_topic(
                    chat_id=self.settings.tg_group_id,
                    message_thread_id=conv["tg_topic_id"],
                    name=f"{STATUS_EMOJI[status]} {conv['client_name']}"[:128])
            except TelegramBadRequest:
                pass

    async def send_reminder(self, conv: dict, stage: int, elapsed_min: int) -> None:
        text = (
            f"⏰ <b>Напоминание #{stage}.</b> Обращение клиента "
            f"<b>{conv['client_name']}</b> без ответа уже ~{elapsed_min} мин."
        )
        if conv.get("tg_topic_id"):
            await self.bot.send_message(self.settings.tg_group_id, text, message_thread_id=conv["tg_topic_id"])
        else:
            await self.bot.send_message(self.settings.tg_group_id, text)

    # ---------- хендлеры ----------

    def _register_handlers(self) -> None:
        r = AiogramRouter()
        self.dp.include_router(r)

        async def deny(m: Message) -> None:
            await m.answer("⛔️ У вас нет доступа к этому боту.")

        @r.message(Command("start"))
        async def cmd_start(m: Message):
            if not self._allowed(m.from_user.id):
                return await deny(m)
            await m.answer(
                f"🌉 <b>{self.settings.brand_name}</b>\n"
                "Сообщения клиентов ВКонтакте приходят в темы этой группы. "
                "Отвечайте прямо в теме — клиент получит ответ во ВКонтакте.\n"
                "Команды: /help /status /stats /health")

        @r.message(Command("help"))
        async def cmd_help(m: Message):
            if not self._allowed(m.from_user.id):
                return await deny(m)
            await m.answer(
                "<b>Как работать:</b>\n"
                "1. Каждому клиенту ВК соответствует отдельная тема.\n"
                "2. Ответ = обычное сообщение в теме клиента (текст, фото, документы).\n"
                "3. Сообщения, начинающиеся с «/» или «!», клиенту не уходят.\n"
                "4. Статусы переключаются кнопками под шапкой темы.\n"
                "5. Бот напомнит, если клиент долго ждёт ответа.\n"
                "6. Если клиент запретил сообщения — бот сообщит об этом.")

        @r.message(Command("status"))
        async def cmd_status(m: Message):
            if not self._admin(m.from_user.id):
                return await deny(m)
            router = self.router_holder.get("router")
            mode = "темы" if self.forum_enabled else "якорные сообщения (без тем)"
            s = await self.db.stats()
            await m.answer(
                f"⚙️ Режим: {mode}\n"
                f"Диалогов: {s['total']} (новых {s['new']}, в работе {s['in_progress']}, "
                f"отвечено {s['answered']}, закрыто {s['closed']})\n"
                f"Очередь исходящих: {s['outbox_pending']}")

        @r.message(Command("stats"))
        async def cmd_stats(m: Message):
            if not self._admin(m.from_user.id):
                return await deny(m)
            s = await self.db.stats()
            await m.answer(
                f"📊 Всего диалогов: {s['total']}\n"
                f"🆕 Новые: {s['new']}  🔄 В работе: {s['in_progress']}\n"
                f"✅ Отвечено: {s['answered']}  📁 Закрыто: {s['closed']}")

        @r.message(Command("health"))
        async def cmd_health(m: Message):
            if not self._admin(m.from_user.id):
                return await deny(m)
            router = self.router_holder.get("router")
            vk_ok = await router.vk.health() if router else False
            tg_ok = True
            try:
                await self.bot.get_me()
            except Exception:
                tg_ok = False
            await m.answer(
                f"🩺 VK API: {'доступен ✅' if vk_ok else 'НЕДОСТУПЕН ❌'}\n"
                f"Telegram API: {'доступен ✅' if tg_ok else 'НЕДОСТУПЕН ❌'}")

        @r.message(Command("forget"))
        async def cmd_forget(m: Message):
            """Удалить данные клиента: /forget в теме или /forget <vk_id> в личке."""
            if not self._admin(m.from_user.id):
                return await deny(m)
            conv = None
            if m.message_thread_id:
                conv = await self.db.get_conversation_by_topic(m.message_thread_id)
            elif m.text and m.text.split()[-1].isdigit():
                conv = await self.db.get_conversation(int(m.text.split()[-1]))
            if conv:
                await self.db.delete_conversation(conv["vk_peer_id"])
                await m.answer(f"🗑 Данные диалога VK {conv['vk_peer_id']} удалены.")
            else:
                await m.answer("Диалог не найден: выполните команду в теме клиента или укажите VK ID.")

        @r.callback_query(F.data.startswith("st:"))
        async def cb_status(q: CallbackQuery):
            if not self._allowed(q.from_user.id):
                await q.answer("Нет доступа", show_alert=True)
                return
            status = q.data.split(":", 1)[1]
            conv = None
            if q.message and q.message.message_thread_id:
                conv = await self.db.get_conversation_by_topic(q.message.message_thread_id)
            elif q.message:
                conv = await self.db.get_conversation_by_tg_message(q.message.message_id)
            if not conv:
                await q.answer("Диалог не найден", show_alert=True)
                return
            await self.db.set_status(conv["vk_peer_id"], status)
            await self.set_status_view(conv, status)
            await q.answer(f"Статус: {STATUS_TITLES[status]}")

        @r.message(F.chat.id == self.settings.tg_group_id)
        async def staff_message(m: Message):
            router = self.router_holder.get("router")
            if router is None:
                return
            if not self._allowed(m.from_user.id):
                return  # молча игнорируем неавторизованных
            # Защита от случайной отправки команд/служебных сообщений клиенту
            if m.text and m.text.startswith(SERVICE_PREFIXES):
                return
            conv = await self._resolve_conversation(m)
            if not conv:
                return  # сообщение вне темы клиента — не отправляем никому
            staff = await self._to_staff_message(m)
            await router.handle_staff_message(conv, staff)

    async def _resolve_conversation(self, m: Message) -> Optional[dict]:
        """Определяет клиента строго по теме или по reply — без угадываний."""
        if m.message_thread_id:
            return await self.db.get_conversation_by_topic(m.message_thread_id)
        if m.reply_to_message:
            return await self.db.get_conversation_by_tg_message(m.reply_to_message.message_id)
        return None

    async def _to_staff_message(self, m: Message) -> StaffMessage:
        staff = StaffMessage(
            tg_user_id=m.from_user.id, tg_message_id=m.message_id,
            text=m.text or m.caption or "")
        if m.photo:
            f = await self.bot.download(m.photo[-1].file_id)
            staff.photos.append(Attachment(kind="photo", file_bytes=f.read()))
        if m.document:
            if m.document.file_size and m.document.file_size > 20 * 1024 * 1024:
                await m.reply("⚠️ Файл больше 20 МБ — Telegram не позволяет боту его скачать.")
            else:
                f = await self.bot.download(m.document.file_id)
                staff.document = Attachment(
                    kind="doc", file_bytes=f.read(),
                    filename=m.document.file_name or "document")
        if m.voice:
            f = await self.bot.download(m.voice.file_id)
            staff.voice = Attachment(kind="voice", file_bytes=f.read(), filename="voice.ogg")
        return staff

    async def run_polling(self) -> None:
        await self.dp.start_polling(
            self.bot, allowed_updates=["message", "callback_query"],
            tasks_shutdown_timeout=30)

    async def close(self) -> None:
        await self.bot.session.close()
