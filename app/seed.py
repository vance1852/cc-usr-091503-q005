"""演示/测试种子数据。"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from .db import init_db
from .timeutil import iso, now_utc


def seed(conn: sqlite3.Connection, now: datetime | None = None) -> dict:
    init_db(conn)
    now = now or now_utc()
    cur = conn.cursor()

    def emp(name, **kw):
        cols = ["name"] + list(kw.keys())
        vals = [name] + list(kw.values())
        placeholders = ",".join("?" * len(cols))
        col_list = ", ".join(cols)
        cur.execute(
            f"INSERT INTO employees ({col_list}) VALUES ({placeholders})",
            vals,
        )
        return cur.lastrowid

    # 三名月嫂：A 资深、B 无早产儿资质、C 负荷高
    a = emp("张姐")
    b = emp("李姐")
    c = emp("王姐")

    def cert(eid, name, vf, vu):
        cur.execute(
            "INSERT INTO certifications (employee_id, name, valid_from, valid_until) VALUES (?,?,?,?)",
            (eid, name, iso(vf), iso(vu)),
        )

    base = now - timedelta(days=30)
    long_later = now + timedelta(days=400)
    cert(a, "母婴护理证", base, long_later)
    cert(a, "早产儿照护培训", base, long_later)
    cert(b, "母婴护理证", base, long_later)  # 无早产儿培训
    cert(c, "母婴护理证", base, long_later)
    cert(c, "早产儿照护培训", base, long_later)

    for eid, skills in ((a, {"早产儿", "双胞胎", "母乳指导"}),
                        (b, {"母乳指导"}),
                        (c, {"早产儿"})):
        for s in skills:
            cur.execute("INSERT INTO skills (employee_id, skill) VALUES (?,?)", (eid, s))

    cur.execute(
        "INSERT INTO families (name, required_certs, preferred_skills, care_summary, notes) "
        "VALUES (?,?,?,?,?)",
        ("早产宝宝家", "早产儿照护培训", "早产儿,母乳指导",
         "34 周早产，喂养需少量多餐，注意体温监测", "家长焦虑，沟通需耐心"),
    )
    fam_prem = cur.lastrowid
    cur.execute(
        "INSERT INTO families (name, required_certs, preferred_skills, care_summary) "
        "VALUES (?,?,?,?)",
        ("普通月子家", "母婴护理证", "母乳指导", "顺产，母婴状况稳定"),
    )
    fam_norm = cur.lastrowid

    cur.execute(
        "INSERT INTO rooms (family_id, isolation) VALUES (?, 'strict')", (fam_prem,))
    room_prem = cur.lastrowid
    cur.execute(
        "INSERT INTO rooms (family_id, isolation) VALUES (?, 'standard')", (fam_norm,))
    room_norm = cur.lastrowid

    # 跨日夜班（22:00 次日 08:00）：A 今晚在早产宝宝家
    tonight = now.replace(hour=22, minute=0, second=0, microsecond=0)
    if tonight < now:
        tonight += timedelta(days=1)
    cur.execute(
        """INSERT INTO assignments
           (employee_id, family_id, room_id, starts_at, ends_at, status, created_at)
           VALUES (?,?,?, ?,?, 'confirmed', ?)""",
        (a, fam_prem, room_prem, iso(tonight), iso(tonight + timedelta(hours=10)), iso(now)),
    )
    target = cur.lastrowid

    conn.commit()
    return {
        "employees": {"a": a, "b": b, "c": c},
        "families": {"prem": fam_prem, "norm": fam_norm},
        "rooms": {"prem": room_prem, "norm": room_norm},
        "tonight": tonight,
        "target_assignment": target,
    }
