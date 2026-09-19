"""资质在班次中途到期 / 资质后来过期只影响尚未开始的安排。"""
from __future__ import annotations

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
    get_qual_id,
    hours_from_now,
    transition,
)

SKILL = "premature_infant_care"


def _seed(client):
    """一名持早产儿照护资质的月嫂 + 需要该技能的家庭。"""
    w = add_worker(client, "王芳", "E001",
                   [(SKILL, dt(-30, 0), dt(60, 0))])
    fam = add_family(client, "陈家", [SKILL])
    return w, fam


def test_mid_shift_expiry_blocks_candidate(client):
    """资质在班次进行中到期 → 候选人硬性排除（QUALIFICATION_EXPIRES_MID_SHIFT）。"""
    w_short = add_worker(client, "李梅", "E002",
                         [(SKILL, dt(-30, 0), dt(1, 13))])  # 资质明天 13:00 到期
    w_long = add_worker(client, "赵敏", "E003",
                        [(SKILL, dt(-30, 0), dt(60, 0))])
    fam = add_family(client, "陈家", [SKILL])
    # 班次 08:00–18:00 跨越资质到期时刻 13:00
    shift = add_shift(client, fam, dt(1, 8), dt(1, 18))

    r = client.get(f"/api/shifts/{shift['id']}/candidates", headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    cands = r.json()["candidates"]

    excluded = {c["worker_id"]: c for c in cands["excluded"]}
    assert w_short in excluded
    codes = [v["code"] for v in excluded[w_short]["hard_violations"]]
    assert "QUALIFICATION_EXPIRES_MID_SHIFT" in codes

    preferred_ids = [c["worker_id"] for c in cands["preferred"]]
    assert w_long in preferred_ids


def test_expire_qualification_marks_only_not_started_shifts(client):
    """资质后来过期：未开始班次列入重排；进行中班次不受影响。"""
    w, fam = _seed(client)
    qual_id = get_qual_id(client, w, SKILL)

    # 进行中班次（现在开始 – 7 小时后）
    in_progress = add_shift(client, fam, hours_from_now(-1), hours_from_now(7),
                            worker_id=w)
    transition(client, in_progress["id"], "start")
    # 未开始班次（明天）
    upcoming = add_shift(client, fam, dt(1, 8), dt(1, 16), worker_id=w)

    r = client.post(f"/api/qualifications/{qual_id}/expire",
                    json={}, headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["affected_count"] == 1
    affected = data["affected_shifts"][0]
    assert affected["shift_id"] == upcoming["id"]
    assert affected["family_name"] == "陈家"
    assert affected["missing_skill"] == SKILL

    # 进行中班次未被标记，人员不变
    r = client.get(f"/api/shifts/{in_progress['id']}", headers=MANAGER_HEADERS)
    assert r.json()["needs_reassignment"] is False
    assert r.json()["worker_id"] == w
    assert r.json()["status"] == "in_progress"

    # 待重排列表只包含未开始班次及受影响家庭
    r = client.get("/api/reassignments-needed", headers=MANAGER_HEADERS)
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["shift"]["id"] == upcoming["id"]
    assert items[0]["family"]["name"] == "陈家"
    assert any(v["code"] in ("QUALIFICATION_NOT_VALID", "QUALIFICATION_EXPIRES_MID_SHIFT")
               for v in items[0]["reasons"])


def test_expire_qualification_mid_future_shift(client):
    """指定到期时刻落在未来班次中途 → 该班次列入重排。"""
    w, fam = _seed(client)
    qual_id = get_qual_id(client, w, SKILL)
    # 班次：明天 08:00–20:00
    upcoming = add_shift(client, fam, dt(1, 8), dt(1, 20), worker_id=w)

    r = client.post(f"/api/qualifications/{qual_id}/expire",
                    json={"effective_at": dt(1, 14).isoformat()},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["affected_count"] == 1
    assert data["affected_shifts"][0]["shift_id"] == upcoming["id"]
    assert "无法覆盖班次" in data["affected_shifts"][0]["reason"]


def test_reassign_clears_needs_reassignment(client):
    """重排后清除标记，待重排列表变空。"""
    w, fam = _seed(client)
    w_ok = add_worker(client, "赵敏", "E002", [(SKILL, dt(-30, 0), dt(60, 0))])
    qual_id = get_qual_id(client, w, SKILL)
    upcoming = add_shift(client, fam, dt(1, 8), dt(1, 16), worker_id=w)

    client.post(f"/api/qualifications/{qual_id}/expire", json={},
                headers=MANAGER_HEADERS)
    r = client.get("/api/reassignments-needed", headers=MANAGER_HEADERS)
    assert len(r.json()["items"]) == 1

    r = client.post(f"/api/shifts/{upcoming['id']}/reassign",
                    json={"worker_id": w_ok}, headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["shift"]["needs_reassignment"] is False

    r = client.get("/api/reassignments-needed", headers=MANAGER_HEADERS)
    assert r.json()["items"] == []
