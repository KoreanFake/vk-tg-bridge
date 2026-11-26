"""Вспомогательные функции: маскирование ПДн, рабочие часы."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

_PHONE_RE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-()]*\d{2}[\s\-()]*\d{2}")
_ADDRESS_RE = re.compile(
    r"(?i)(?:ул\.?|улица|просп\.?|проспект|пр-т|пр\.?|пер\.?|переулок|шоссе|наб\.?|набережная|б-р|бульвар)[^\n,]{0,60}"
)


def mask_personal_data(text: str) -> str:
    """Скрывает телефоны и фрагменты, похожие на адреса доставки."""
    text = _PHONE_RE.sub("[телефон скрыт]", text)
    text = _ADDRESS_RE.sub("[адрес скрыт]", text)
    return text


def in_work_hours(work_hours: str, now: Optional[datetime] = None) -> bool:
    """work_hours: '09:00-21:00' или пустая строка (круглосуточно)."""
    if not work_hours:
        return True
    now = now or datetime.now()
    try:
        start_s, end_s = work_hours.split("-")
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, end_s.split(":"))
    except ValueError:
        return True
    cur = now.hour * 60 + now.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end  # интервал через полночь
