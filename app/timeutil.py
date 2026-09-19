"""时间工具：统一以带时区的 UTC datetime 处理，存储为 ISO8601（Z 结尾）字符串。"""
from __future__ import annotations

from datetime import datetime, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def parse_dt(value: str | datetime) -> datetime:
    """解析 ISO8601 字符串；接受 Z 结尾。无时区输入按 UTC 处理，统一返回 UTC aware datetime。"""
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return parse_dt(dt).strftime(ISO_FMT)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
