"""数据访问：封装编排所需的查询。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Optional

from .timeutil import iso

LOOKOUT = timedelta(hours=72)  # 评估候选时向前后各取 3 天班次


def _split(value: Optional[str]) -> set[str]:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


class Repo:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---------- 基础实体 ----------
    def get_employee(self, employee_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM employees WHERE id = ?", (employee_id,)
        ).fetchone()

    def list_employees(self, active_only: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM employees"
        if active_only:
            sql += " WHERE active = 1"
        return list(self.conn.execute(sql + " ORDER BY id").fetchall())

    def get_family(self, family_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM families WHERE id = ?", (family_id,)
        ).fetchone()

    def get_assignment(self, assignment_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, e.name AS employee_name, f.name AS family_name
               FROM assignments a
               JOIN employees e ON e.id = a.employee_id
               JOIN families f ON f.id = a.family_id
               WHERE a.id = ?""",
            (assignment_id,),
        ).fetchone()

    def get_room(self, room_id: Optional[int]) -> Optional[sqlite3.Row]:
        if room_id is None:
            return None
        return self.conn.execute(
            "SELECT * FROM rooms WHERE id = ?", (room_id,)
        ).fetchone()

    def room_for_family(self, family_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM rooms WHERE family_id = ? ORDER BY id LIMIT 1",
            (family_id,),
        ).fetchone()

    def certifications(self, employee_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM certifications WHERE employee_id = ?",
            (employee_id,),
        ).fetchall())

    def skills(self, employee_id: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT skill FROM skills WHERE employee_id = ?", (employee_id,)
        ).fetchall()
        return {r["skill"] for r in rows}

    def confirmed_assignments_between(
        self, start: datetime, end: datetime, exclude_id: Optional[int] = None
    ) -> list[sqlite3.Row]:
        sql = """SELECT a.*, e.name AS employee_name, f.name AS family_name,
                        f.care_summary AS care_summary
                 FROM assignments a
                 JOIN employees e ON e.id = a.employee_id
                 JOIN families f ON f.id = a.family_id
                 WHERE a.status = 'confirmed'
                   AND a.starts_at < ? AND ? < a.ends_at"""
        params: list = [iso(end), iso(start)]
        if exclude_id is not None:
            sql += " AND a.id != ?"
            params.append(exclude_id)
        sql += " ORDER BY a.starts_at"
        return list(self.conn.execute(sql, params).fetchall())

    def employee_assignments(
        self, employee_id: int, lo: datetime, hi: datetime, exclude_id: Optional[int] = None
    ) -> list[sqlite3.Row]:
        sql = """SELECT a.*, e.name AS employee_name, f.name AS family_name,
                        f.care_summary AS care_summary
                 FROM assignments a
                 JOIN employees e ON e.id = a.employee_id
                 JOIN families f ON f.id = a.family_id
                 WHERE a.status = 'confirmed' AND a.employee_id = ?
                   AND a.starts_at < ? AND ? < a.ends_at"""
        params: list = [employee_id, iso(hi), iso(lo)]
        if exclude_id is not None:
            sql += " AND a.id != ?"
            params.append(exclude_id)
        sql += " ORDER BY a.starts_at"
        return list(self.conn.execute(sql, params).fetchall())

    def served_family_before(self, employee_id: int, family_id: int,
                             before: datetime, exclude_id: Optional[int]) -> bool:
        sql = """SELECT 1 FROM assignments
                 WHERE status = 'confirmed' AND employee_id = ? AND family_id = ?
                   AND starts_at < ?"""
        params: list = [employee_id, family_id, iso(before)]
        if exclude_id is not None:
            sql += " AND id != ?"
            params.append(exclude_id)
        return self.conn.execute(sql, params).fetchone() is not None

    # ---------- 写操作 ----------
    def supersede_and_create(
        self, old_id: int, to_employee_id: int, family_id: int,
        room_id: Optional[int], start: datetime, end: datetime, now: datetime
    ) -> int:
        self.conn.execute(
            "UPDATE assignments SET status = 'superseded' WHERE id = ?",
            (old_id,),
        )
        cur = self.conn.execute(
            """INSERT INTO assignments
               (employee_id, family_id, room_id, starts_at, ends_at, status,
                supersedes_id, created_at)
               VALUES (?, ?, ?, ?, ?, 'confirmed', ?, ?)""",
            (to_employee_id, family_id, room_id, iso(start), iso(end),
             old_id, iso(now)),
        )
        return int(cur.lastrowid)

    def create_handoff(self, assignment_id: int, from_emp: int, to_emp: int,
                       now: datetime) -> int:
        cur = self.conn.execute(
            """INSERT INTO handoffs
               (assignment_id, from_employee_id, to_employee_id, created_at)
               VALUES (?, ?, ?, ?)""",
            (assignment_id, from_emp, to_emp, iso(now)),
        )
        return int(cur.lastrowid)

    def get_handoff(self, handoff_id: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM handoffs WHERE id = ?", (handoff_id,)
        ).fetchone()

    def find_acknowledged_handoff(self, assignment_id: int, to_emp: int) -> Optional[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM handoffs
               WHERE assignment_id = ? AND to_employee_id = ?
                 AND acknowledged_at IS NOT NULL
               ORDER BY acknowledged_at DESC LIMIT 1""",
            (assignment_id, to_emp),
        ).fetchone()

    def acknowledge_handoff(self, handoff_id: int, now: datetime) -> None:
        self.conn.execute(
            "UPDATE handoffs SET acknowledged_at = ? WHERE id = ?",
            (iso(now), handoff_id),
        )

    # ---------- 资质到期扫描 ----------
    def future_assignments(self, now: datetime) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            """SELECT a.*, e.name AS employee_name, f.name AS family_name,
                      f.care_summary AS care_summary
               FROM assignments a
               JOIN employees e ON e.id = a.employee_id
               JOIN families f ON f.id = a.family_id
               WHERE a.status = 'confirmed' AND a.starts_at > ?
               ORDER BY a.employee_id, a.starts_at""",
            (iso(now),),
        ).fetchall())
