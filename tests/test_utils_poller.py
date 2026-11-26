"""Тесты разбора событий Bots Long Poll и утилит."""
from __future__ import annotations

from app.utils import in_work_hours, mask_personal_data
from app.vk_poller import parse_message_event


def test_parse_object_format():
    upd = {"type": "message_new", "object": {"message": {
        "id": 10, "peer_id": 555, "from_id": 555, "date": 1700000000,
        "text": "Привет", "conversation_message_id": 7,
        "attachments": [{"type": "photo", "photo": {"sizes": [
            {"type": "x", "url": "http://x/s.jpg"}, {"type": "w", "url": "http://x/l.jpg"}]}}]}}}
    msg = parse_message_event(upd)
    assert msg and msg.peer_id == 555 and not msg.outgoing
    assert msg.attachments[0].kind == "photo" and msg.attachments[0].url.endswith("l.jpg")


def test_parse_outgoing():
    upd = {"type": "message_new", "object": {"message": {
        "id": 11, "peer_id": 555, "from_id": -12345, "date": 1, "text": "Ответ",
        "conversation_message_id": 8}}}
    msg = parse_message_event(upd)
    assert msg.outgoing is True


def test_parse_classic_numeric_format():
    upd = {"type": 4, "object": [10, 2, 555, 1700000000, "Эхо", {}, {}]}
    msg = parse_message_event(upd)
    assert msg and msg.outgoing and msg.text == "Эхо"


def test_ignore_non_message():
    assert parse_message_event({"type": "message_typing_state"}) is None


def test_dedup_key():
    upd = {"type": "message_new", "object": {"message": {
        "id": 10, "peer_id": 555, "from_id": 555, "date": 1, "text": "",
        "conversation_message_id": 7}}}
    assert parse_message_event(upd).dedup_key == "vk:555:7"


def test_masking():
    text = "Мой телефон +7 900 123-45-67, привезите на ул. Ленина, д. 5"
    masked = mask_personal_data(text)
    assert "900" not in masked and "Ленина" not in masked
    assert "[телефон скрыт]" in masked and "[адрес скрыт]" in masked


def test_work_hours():
    from datetime import datetime
    assert in_work_hours("", datetime(2026, 1, 1, 3, 0))
    assert in_work_hours("09:00-21:00", datetime(2026, 1, 1, 12, 0))
    assert not in_work_hours("09:00-21:00", datetime(2026, 1, 1, 23, 0))
    assert in_work_hours("22:00-06:00", datetime(2026, 1, 1, 23, 0))  # через полночь
