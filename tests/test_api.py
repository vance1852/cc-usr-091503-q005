"""API 测试：角色隔离、交接流程、并发调班。"""
from __future__ import annotations

import threading
from datetime import timedelta

import pytest

from app.timeutil import iso

MANAGER = {"X-User-Role": "manager"}
NANNY = {"X-User-Role": "nanny"}


def test_manager_sees_explanations(seeded_client):
    client, d = seeded_client
    r = client.get(f"/api/assignments/{d['target_assignment']}/adjustment", headers=MANAGER)
    assert r.status_code == 200
    body = r.json()
    assert body["family_name"] == "早产宝宝家"
    assert body["required_handoff"] is False
    cand_b = next(c for c in body["candidates"] if c["employee_id"] == d["employees"]["b"])
    assert cand_b["eligible"] is False
    assert any(i["code"] == "CERT_MISSING" and i["severity"] == "hard"
               for i in cand_b["issues"])


def test_nanny_forbidden_from_manager_endpoints(seeded_client):
    client, d = seeded_client
    r = client.get(f"/api/assignments/{d['target_assignment']}/adjustment", headers=NANNY)
    assert r.status_code == 403


def test_nanny_sees_only_own_schedule_and_care_summary(seeded_client):
    """月嫂只可查看自己的安排及照护摘要，看不到经理备注与他人安排。"""
    client, d = seeded_client
    tonight = d["tonight"]
    a = d["employees"]["a"]
    headers = {**NANNY, "X-User-Id": str(a)}
    r = client.get(
        "/api/me/schedule",
        params={"since": iso(tonight - timedelta(days=1)),
                "until": iso(tonight + timedelta(days=2))},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["employee_id"] == a
    assert len(body["assignments"]) == 1
    asg = body["assignments"][0]
    assert "34 周早产" in asg["care_summary"]
    assert "notes" not in asg and "备注" not in str(body)


def test_nanny_requires_id(seeded_client):
    client, d = seeded_client
    r = client.get("/api/me/schedule",
                   params={"since": iso(d["tonight"]), "until": iso(d["tonight"] + timedelta(hours=1))},
                   headers=NANNY)
    assert r.status_code == 403


def test_full_future_reassignment_flow(seeded_client):
    client, d = seeded_client
    c = d["employees"]["c"]
    r = client.post("/api/reassignments",
                    json={"assignment_id": d["target_assignment"], "to_employee_id": c},
                    headers=MANAGER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] is True
    # 再调同一班：已 superseded → 409
    r2 = client.post("/api/reassignments",
                     json={"assignment_id": d["target_assignment"], "to_employee_id": c},
                     headers=MANAGER)
    assert r2.status_code == 409


def test_hard_candidate_rejected_via_api(seeded_client):
    client, d = seeded_client
    r = client.post("/api/reassignments",
                    json={"assignment_id": d["target_assignment"],
                          "to_employee_id": d["employees"]["b"]},
                    headers=MANAGER)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "rule_violation"


def test_in_progress_via_live_shift(seeded_client):
    """构造一个当前正在进行的班次，验证静默换人被拒、交接后放行。"""
    client, d = seeded_client
    conn = client.app.state.conn
    from app.timeutil import now_utc
    now = now_utc().replace(microsecond=0)
    # B 当前正在普通月子家服务（进行中），拟紧急接替给 C
    cur = conn.execute(
        """INSERT INTO assignments
           (employee_id, family_id, room_id, starts_at, ends_at, status, created_at)
           VALUES (?,?,?, ?,?, 'confirmed', ?)""",
        (d["employees"]["b"], d["families"]["norm"], d["rooms"]["norm"],
         iso(now - timedelta(hours=2)), iso(now + timedelta(hours=6)), iso(now)),
    )
    live_id = cur.lastrowid
    conn.commit()

    c = d["employees"]["c"]
    r = client.post("/api/reassignments",
                    json={"assignment_id": live_id, "to_employee_id": c},
                    headers=MANAGER)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "handoff_required"

    r = client.post("/api/handoffs",
                    json={"assignment_id": live_id, "to_employee_id": c},
                    headers=MANAGER)
    assert r.status_code == 200
    hid = r.json()["handoff_id"]

    # 未确认时仍被拒
    r = client.post("/api/reassignments",
                    json={"assignment_id": live_id, "to_employee_id": c},
                    headers=MANAGER)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "handoff_required"

    r = client.post(f"/api/handoffs/{hid}/acknowledge", headers=MANAGER)
    assert r.status_code == 200

    r = client.post("/api/reassignments",
                    json={"assignment_id": live_id, "to_employee_id": c},
                    headers=MANAGER)
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True
    assert r.json()["required_handoff"] is True


def test_concurrent_reassignment_only_one_wins(seeded_client):
    """并发调班：两个经理同时把同一空档员工排进重叠时段，只允许一个成功。"""
    client, d = seeded_client
    conn = client.app.state.conn
    tonight = d["tonight"]
    c = d["employees"]["c"]

    # 第二个待调班：B 在与目标班完全重叠的时段服务另一个严格隔离房间家庭
    cur = conn.execute(
        """INSERT INTO assignments
           (employee_id, family_id, room_id, starts_at, ends_at, status, created_at)
           VALUES (?,?,?, ?,?, 'confirmed', ?)""",
        (d["employees"]["b"], d["families"]["prem"], d["rooms"]["prem"],
         iso(tonight), iso(tonight + timedelta(hours=10)), iso(d["tonight"])),
    )
    second_id = cur.lastrowid
    conn.commit()

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list = []

    def do_reassign(assignment_id):
        try:
            barrier.wait(timeout=10)
            r = client.post("/api/reassignments",
                            json={"assignment_id": assignment_id, "to_employee_id": c},
                            headers=MANAGER)
            results.append(r.status_code)
        except Exception as e:  # pragma: no cover
            errors.append(e)

    t1 = threading.Thread(target=do_reassign, args=(d["target_assignment"],))
    t2 = threading.Thread(target=do_reassign, args=(second_id,))
    t1.start(); t2.start()
    t1.join(10); t2.join(10)

    assert not errors
    assert sorted(results) == [200, 409]
    # C 在该时段最终只有一个 confirmed 班次
    rows = conn.execute(
        """SELECT COUNT(*) FROM assignments
           WHERE employee_id=? AND status='confirmed'
             AND starts_at < ? AND ? < ends_at""",
        (c, iso(tonight + timedelta(hours=10)), iso(tonight)),
    ).fetchone()
    assert rows[0] == 1


def test_db_trigger_blocks_overlapping_insert(seeded_client):
    """即使绕过引擎，SQLite 触发器也阻止同一员工重叠排班。"""
    client, d = seeded_client
    conn = client.app.state.conn
    tonight = d["tonight"]
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO assignments
               (employee_id, family_id, room_id, starts_at, ends_at, status, created_at)
               VALUES (?,?,?, ?,?, 'confirmed', ?)""",
            (d["employees"]["a"], d["families"]["norm"], d["rooms"]["norm"],
             iso(tonight + timedelta(hours=1)), iso(tonight + timedelta(hours=3)),
             iso(tonight)),
        )


def test_expiring_certifications_endpoint(seeded_client):
    client, d = seeded_client
    conn = client.app.state.conn
    tonight = d["tonight"]
    conn.execute(
        "UPDATE certifications SET valid_until=? WHERE employee_id=? AND name='早产儿照护培训'",
        (iso(tonight + timedelta(hours=2)), d["employees"]["a"]),
    )
    conn.commit()
    r = client.get("/api/manager/expiring-certifications", headers=MANAGER)
    assert r.status_code == 200
    ids = [a["id"] for it in r.json()["items"] for a in it["affected_assignments"]]
    assert d["target_assignment"] in ids
