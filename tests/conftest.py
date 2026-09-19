"""测试共享夹具与数据构造辅助。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

UTC = timezone.utc
MANAGER_HEADERS = {"X-Role": "manager"}


def worker_headers(worker_id: int) -> dict:
    return {"X-Role": "worker", "X-Worker-Id": str(worker_id)}


def dt(day_offset: int, hour: int, minute: int = 0) -> datetime:
    """以今日 0 点（UTC）为基准的测试时刻，避免硬编码日期。"""
    base = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(days=day_offset, hours=hour, minutes=minute)


def hours_from_now(hours: float) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


@pytest.fixture()
def app(tmp_path):
    return create_app(f"sqlite:///{tmp_path}/test.db")


@pytest.fixture()
def client(app):
    with TestClient(app) as c:
        yield c


# ---- 数据构造辅助 ----

def add_worker(client, name: str, employee_no: str, skills=()) -> int:
    """skills: [(skill, valid_from, valid_until), ...]"""
    r = client.post("/api/workers", json={"name": name, "employee_no": employee_no},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 201, r.text
    wid = r.json()["id"]
    for skill, valid_from, valid_until in skills:
        r = client.post(
            f"/api/workers/{wid}/qualifications",
            json={"skill": skill,
                  "valid_from": valid_from.isoformat(),
                  "valid_until": valid_until.isoformat()},
            headers=MANAGER_HEADERS,
        )
        assert r.status_code == 201, r.text
    return wid


def get_qual_id(client, worker_id: int, skill: str) -> int:
    r = client.get("/api/workers", headers=MANAGER_HEADERS)
    for w in r.json():
        if w["id"] == worker_id:
            for q in w["qualifications"]:
                if q["skill"] == skill:
                    return q["id"]
    raise AssertionError(f"worker {worker_id} 没有资质 {skill}")


def add_family(client, name: str, required_skills=(), isolation=False,
               room="101", notes="") -> int:
    r = client.post("/api/families",
                    json={"name": name, "room": room,
                          "isolation_required": isolation,
                          "required_skills": list(required_skills),
                          "care_notes": notes},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def add_shift(client, family_id: int, start: datetime, end: datetime,
              worker_id: int | None = None, approve: bool = False,
              expect: int = 201) -> dict:
    payload = {"family_id": family_id,
               "start_at": start.isoformat(), "end_at": end.isoformat(),
               "approve": approve}
    if worker_id is not None:
        payload["worker_id"] = worker_id
    r = client.post("/api/shifts", json=payload, headers=MANAGER_HEADERS)
    assert r.status_code == expect, r.text
    return r.json()


def transition(client, shift_id: int, action: str, expect: int = 200) -> dict:
    r = client.post(f"/api/shifts/{shift_id}/transition/{action}",
                    headers=MANAGER_HEADERS)
    assert r.status_code == expect, r.text
    return r.json()
