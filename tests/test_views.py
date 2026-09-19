"""角色视图：经理看全局与评估依据；月嫂只看自己的安排及照护摘要。"""
from __future__ import annotations

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
    worker_headers,
)


def _seed(client):
    w1 = add_worker(client, "王芳", "E001",
                    [("premature_infant_care", dt(-30, 0), dt(60, 0))])
    w2 = add_worker(client, "赵敏", "E002")
    fam1 = add_family(client, "陈家", ["premature_infant_care"],
                      isolation=True, room="VIP-1",
                      notes="早产儿，需监测喂养，房间隔离")
    fam2 = add_family(client, "林家", room="302")
    s1 = add_shift(client, fam1, dt(1, 8), dt(1, 20), worker_id=w1)
    s2 = add_shift(client, fam2, dt(2, 8), dt(2, 12), worker_id=w2)
    return w1, w2, s1, s2


def test_worker_cannot_access_manager_endpoints(client):
    w1, _, s1, _ = _seed(client)
    for method, url in [
        ("GET", f"/api/shifts/{s1['id']}/candidates"),
        ("GET", "/api/workers"),
        ("GET", "/api/reassignments-needed"),
        ("POST", "/api/leave"),
        ("POST", f"/api/shifts/{s1['id']}/reassign"),
    ]:
        r = client.request(method, url, json={}, headers=worker_headers(w1))
        assert r.status_code == 403, f"{method} {url} 应拒绝月嫂访问"


def test_worker_sees_only_own_schedule_with_care_summary(client):
    w1, w2, s1, s2 = _seed(client)

    r = client.get("/api/my/schedule", headers=worker_headers(w1))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["worker"]["id"] == w1

    shifts = data["shifts"]
    assert [s["id"] for s in shifts] == [s1["id"]]  # 不含赵敏的班次

    summary = shifts[0]["care_summary"]
    assert summary["family_name"] == "陈家"
    assert summary["room"] == "VIP-1"
    assert summary["isolation_required"] is True
    assert summary["required_skills"] == ["premature_infant_care"]
    assert "早产儿" in summary["care_notes"]

    # 月嫂视图不泄露编排信息（候选评估、其他月嫂）
    assert "candidates" not in shifts[0]
    assert "evaluation" not in shifts[0]

    # 赵敏的视图里也没有王芳的班次
    r = client.get("/api/my/schedule", headers=worker_headers(w2))
    assert [s["id"] for s in r.json()["shifts"]] == [s2["id"]]


def test_worker_cannot_impersonate_other_worker(client):
    """月嫂角色即使带 worker_id 参数也只能看自己。"""
    w1, w2, s1, _ = _seed(client)
    r = client.get("/api/my/schedule", params={"worker_id": w2},
                   headers=worker_headers(w1))
    assert r.status_code == 200
    assert r.json()["worker"]["id"] == w1
    assert [s["id"] for s in r.json()["shifts"]] == [s1["id"]]


def test_manager_can_view_any_schedule(client):
    w1, _, s1, _ = _seed(client)
    r = client.get("/api/my/schedule", params={"worker_id": w1},
                   headers=MANAGER_HEADERS)
    assert r.status_code == 200
    assert [s["id"] for s in r.json()["shifts"]] == [s1["id"]]


def test_isolation_conflict_is_hard_violation(client):
    """隔离家庭班次前后缓冲期内，同一名月嫂不得安排其他家庭。"""
    w = add_worker(client, "王芳", "E001")
    fam_iso = add_family(client, "陈家", isolation=True)
    fam_other = add_family(client, "林家")
    # 隔离班次：明天 08:00–20:00（缓冲至 04:00–24:00）
    add_shift(client, fam_iso, dt(1, 8), dt(1, 20), worker_id=w)
    # 另一家庭的班次：当天 22:00 – 明天 06:00，落入隔离缓冲窗口
    open_shift = add_shift(client, fam_other, dt(1, 22), dt(2, 6))

    r = client.get(f"/api/shifts/{open_shift['id']}/candidates",
                   headers=MANAGER_HEADERS)
    excluded = {c["worker_id"]: c for c in r.json()["candidates"]["excluded"]}
    assert w in excluded
    assert any(v["code"] == "ISOLATION_CONFLICT"
               for v in excluded[w]["hard_violations"])
