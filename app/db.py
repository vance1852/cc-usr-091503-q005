"""SQLite 数据层：员工、资质、技能、家庭需求、班次、交接确认。

写操作通过 BEGIN IMMEDIATE 事务串行化，保证并发调班下不会出现时间段重叠。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    max_consecutive_hours REAL NOT NULL DEFAULT 26,
    max_hours_per_24h REAL NOT NULL DEFAULT 18,
    rest_min_hours REAL NOT NULL DEFAULT 8,
    rest_pref_hours REAL NOT NULL DEFAULT 11,
    consecutive_warn_hours REAL NOT NULL DEFAULT 22
);

CREATE TABLE IF NOT EXISTS certifications (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    name TEXT NOT NULL,            -- 资质名称，如 母婴护理证、早产儿照护培训
    valid_from TEXT NOT NULL,      -- ISO8601 UTC
    valid_until TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    skill TEXT NOT NULL,           -- 专项技能，如 早产儿、双胞胎、母乳指导
    PRIMARY KEY (employee_id, skill)
);

CREATE TABLE IF NOT EXISTS families (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    required_certs TEXT NOT NULL DEFAULT '',   -- 逗号分隔的必需资质
    preferred_skills TEXT NOT NULL DEFAULT '', -- 逗号分隔的偏好技能
    care_summary TEXT NOT NULL DEFAULT '',     -- 月嫂可见的照护摘要
    notes TEXT NOT NULL DEFAULT ''             -- 仅经理可见的备注
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    family_id INTEGER NOT NULL REFERENCES families(id),
    isolation TEXT NOT NULL DEFAULT 'standard' -- none / standard / strict
);

CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    employee_id INTEGER NOT NULL REFERENCES employees(id),
    family_id INTEGER NOT NULL REFERENCES families(id),
    room_id INTEGER REFERENCES rooms(id),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'confirmed',  -- confirmed / superseded / cancelled
    supersedes_id INTEGER REFERENCES assignments(id),
    created_at TEXT NOT NULL,
    CHECK (ends_at > starts_at)
);

-- 同一员工在同一时段不得重复承担服务（仅未失效班次参与）。
-- 重叠条件：a.starts_at < b.ends_at AND b.starts_at < a.ends_at
CREATE TRIGGER IF NOT EXISTS trg_no_overlap_insert
BEFORE INSERT ON assignments
WHEN NEW.status IN ('confirmed')
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1 FROM assignments a
        WHERE a.employee_id = NEW.employee_id
          AND a.status = 'confirmed'
          AND a.id IS NOT NEW.supersedes_id
          AND a.starts_at < NEW.ends_at
          AND NEW.starts_at < a.ends_at
    ) THEN RAISE(ABORT, 'employee already serving during overlapping interval') END;
END;

CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY,
    assignment_id INTEGER NOT NULL REFERENCES assignments(id),
    from_employee_id INTEGER NOT NULL REFERENCES employees(id),
    to_employee_id INTEGER NOT NULL REFERENCES employees(id),
    acknowledged_at TEXT,                      -- NULL = 尚未交接确认
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_asg_emp_time ON assignments(employee_id, starts_at, ends_at);
CREATE INDEX IF NOT EXISTS idx_asg_family ON assignments(family_id);
"""

_lock = threading.RLock()


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    with _lock:
        conn.executescript(SCHEMA)
        conn.commit()


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即获取写锁的事务，串行化并发调班。"""
    with _lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
