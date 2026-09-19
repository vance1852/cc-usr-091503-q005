"""数据库连接。写事务采用 BEGIN IMMEDIATE，保证并发调班串行化。"""
from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker


def make_engine(db_url: str):
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    # BEGIN IMMEDIATE：事务第一条语句即取得 SQLite 写锁，
    # 使"检查冲突 → 写入"在并发调班下串行执行，
    # 从数据库层面保证同一月嫂同一时间段不会被重复排班。
    @event.listens_for(engine, "begin")
    def _begin_immediate(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def make_session_factory(engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
