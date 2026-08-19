from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any


BEIJING_TIMEZONE = timezone(timedelta(hours=8), name="Asia/Shanghai")


def parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=BEIJING_TIMEZONE)

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(BEIJING_TIMEZONE)


def format_beijing_time(value: Any, *, include_seconds: bool = True) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "-"
    pattern = "%Y-%m-%d %H:%M:%S" if include_seconds else "%Y-%m-%d %H:%M"
    return parsed.strftime(pattern)


def beijing_iso_time(value: Any) -> str | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    return parsed.isoformat(timespec="seconds")
