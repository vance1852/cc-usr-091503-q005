"""时间工具：统一以 naive UTC 存入 SQLite，接口层再附加时区。"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（naive，便于 SQLite 存储与比较）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_store(dt: datetime) -> datetime:
    """接口输入（可带时区）转换为 naive UTC 入库。"""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def to_api(dt: datetime | None) -> datetime | None:
    """出库时间附加 UTC 时区后再序列化。"""
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc)


def fmt(dt: datetime) -> str:
    """给人看的短时间格式，用于解释性文案。"""
    return dt.strftime("%m-%d %H:%M")
