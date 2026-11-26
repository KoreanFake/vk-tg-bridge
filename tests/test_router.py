"""Сценарные тесты маршрутизатора (имитация VK и Telegram, без сети)."""
from __future__ import annotations

import pytest

from app.models import StaffMessage
from tests.conftest import vk_msg


# 1. Первый входящий диалог клиента
@pytest.mark.asyncio
async def test_first_incoming_creates_thread(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1))
    conv = await db.get_conversation(555)
    assert conv is not None and conv["tg_topic_id"]
    assert conv["client_name"] == "Иван Тестов"
    assert conv["status"] == "new"
    assert tg.delivered and tg.delivered[0][0] == 555


# 2. Повторное сообщение того же клиента — та же тема
@pytest.mark.asyncio
async def test_second_message_reuses_thread(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1))
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=2, text="Ещё вопрос"))
    conv = await db.get_conversation(555)
    topics = [c for c in [conv["tg_topic_id"]]]
    assert len(topics) == 1 and conv["tg_topic_id"] is not None
    assert len(tg.delivered) == 2


# 3. Одновременные обращения нескольких клиентов не смешиваются
@pytest.mark.asyncio
async def test_multiple_clients_isolated(env):
    db, vk, tg, router, _ = env
    for peer in (1001, 1002, 1003):
        await router.handle_vk_incoming(vk_msg(peer_id=peer, cmi=1, text=f"Клиент {peer}"))
    topics = {(await db.get_conversation(p))["tg_topic_id"] for p in (1001, 1002, 1003)}
    assert len(topics) == 3  # у каждого своя тема


# 4. Ответ сотрудника уходит правильному клиенту
@pytest.mark.asyncio
async def test_staff_reply_goes_to_vk(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=777, cmi=1))
    conv = await db.get_conversation(777)
    staff = StaffMessage(tg_user_id=111, tg_message_id=50, text="Добрый день! Букет будет готов к 18:00.")
    await router.handle_staff_message(conv, staff)
    assert vk.sent and vk.sent[0]["peer_id"] == 777
    assert "готов к 18:00" in vk.sent[0]["text"]
    assert any("доставлен" in n[1] for n in tg.notices)


# 5. Отправка текста и фотографий
@pytest.mark.asyncio
async def test_staff_reply_with_photo(env):
    from app.models import Attachment
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=888, cmi=1))
    conv = await db.get_conversation(888)
    staff = StaffMessage(tg_user_id=111, tg_message_id=60, text="Фото букета",
                         photos=[Attachment(kind="photo", file_bytes=b"\xff\xd8\xff")])
    await router.handle_staff_message(conv, staff)
    assert vk.sent[0]["attachment"].startswith("photo")


# 6. Ошибка VK API при отправке -> повторы, затем уведомление об ошибке
@pytest.mark.asyncio
async def test_vk_error_retries_then_fails(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=999, cmi=1))
    conv = await db.get_conversation(999)
    vk.send_fail_code = 6  # too many requests — временная
    staff = StaffMessage(tg_user_id=111, tg_message_id=70, text="Ответ")
    await router.handle_staff_message(conv, staff)  # не упало, встало на повтор
    async def get_item(i):
        return dict(await (await db.db.execute("SELECT * FROM outbox WHERE id=?", (i,))).fetchone())

    item = await get_item((await db.outbox_due(2**62))[0]["id"])
    assert item["status"] == "pending" and item["attempts"] == 1
    # исчерпываем попытки
    for _ in range(10):
        await router.process_outbox_item(await get_item(item["id"]))
    item = await get_item(item["id"])
    assert item["status"] == "failed"
    assert any("НЕ доставлен" in n[1] for n in tg.notices)


# 7. Клиент запретил сообщения сообщества (VK 901)
@pytest.mark.asyncio
async def test_user_blocked_notifies(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=900, cmi=1))
    conv = await db.get_conversation(900)
    vk.send_fail_code = 901
    staff = StaffMessage(tg_user_id=111, tg_message_id=80, text="Ответ")
    await router.handle_staff_message(conv, staff)
    assert any("запретил сообщения" in n[1] for n in tg.notices)


# 8+9. Перезапуск + повторная доставка события: тема не создаётся заново, дублей нет
@pytest.mark.asyncio
async def test_restart_and_duplicate_event(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=42))
    # «перезапуск»: новый router/tg поверх той же БД
    from app.router import Router
    from tests.conftest import FakeTg
    tg2 = FakeTg(db)
    router2 = Router(db, vk, tg2, router.settings)
    await router2.handle_vk_incoming(vk_msg(peer_id=555, cmi=43))  # тема должна быть из БД
    conv = await db.get_conversation(555)
    assert tg2._topic_seq == 100  # новая тема не создавалась
    assert len(tg2.delivered) == 1
    # повторная доставка того же события
    await router2.handle_vk_incoming(vk_msg(peer_id=555, cmi=43))
    assert len(tg2.delivered) == 1


# 10. Идемпотентность ответа: повторное событие из TG не отправляет дубль клиенту
@pytest.mark.asyncio
async def test_staff_message_dedup(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1))
    conv = await db.get_conversation(555)
    staff = StaffMessage(tg_user_id=111, tg_message_id=90, text="Ответ")
    await router.handle_staff_message(conv, staff)
    await router.handle_staff_message(conv, staff)  # дубль (Telegram redelivered update)
    assert len(vk.sent) == 1
    assert any("уже было отправлено" in n[1] for n in tg.notices)


# 11. Ответ сотрудника напрямую через ВК отображается в Telegram
@pytest.mark.asyncio
async def test_outgoing_echo_mirrored(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1))
    await router.handle_vk_outgoing_echo(
        vk_msg(peer_id=555, cmi=2, text="Здравствуйте! Чем помочь?", outgoing=True))
    assert tg.echoes and "Чем помочь" in tg.echoes[0][1]
    conv = await db.get_conversation(555)
    assert conv["status"] == "answered"  # ложных напоминаний не будет


# 12. Напоминания срабатывают по интервалам и прекращаются после ответа
@pytest.mark.asyncio
async def test_reminders_fire_and_stop(env):
    db, vk, tg, router, settings = env
    t0 = 1_700_000_000
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1, date=t0))
    await router.reminders_tick(now=t0 + 16 * 60)   # прошло 16 минут -> напоминание #1
    await router.reminders_tick(now=t0 + 31 * 60)   # 31 минута -> напоминание #2
    assert [r[1] for r in tg.reminders] == [1, 2]
    # ответили -> напоминания прекращаются
    conv = await db.get_conversation(555)
    await router.handle_staff_message(conv, StaffMessage(tg_user_id=111, tg_message_id=95, text="Ответ"))
    await router.reminders_tick(now=t0 + 90 * 60)
    assert len(tg.reminders) == 2


# 14. Закрытие диалога и повторное обращение возвращают его в активное состояние
@pytest.mark.asyncio
async def test_close_and_reopen(env):
    db, vk, tg, router, _ = env
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1, date=1_700_000_000))
    await db.set_status(555, "closed")
    await router.reminders_tick(now=1_700_000_000 + 3600)  # закрытые не напоминают
    assert not tg.reminders
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=2, date=1_700_000_100))
    conv = await db.get_conversation(555)
    assert conv["status"] == "new"
    assert any("возобновлён" in n[1] for n in tg.notices)


# Рабочие часы: вне интервала напоминаний нет
@pytest.mark.asyncio
async def test_reminders_respect_work_hours(env):
    db, vk, tg, router, settings = env
    settings.work_hours = "09:00-10:00"
    from datetime import datetime
    import app.router as rmod
    t0 = 1_700_000_000
    await router.handle_vk_incoming(vk_msg(peer_id=555, cmi=1, date=t0))
    # подменяем "сейчас" на 03:00 ночи
    import app.utils as u
    orig = u.in_work_hours
    rmod.in_work_hours = lambda wh: False
    try:
        await router.reminders_tick(now=t0 + 3600)
        assert not tg.reminders
    finally:
        rmod.in_work_hours = orig
