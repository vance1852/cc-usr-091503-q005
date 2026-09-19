"""候选评估三档分类：硬性不符合 / 需主管批准 / 普通偏好。

还原题目场景：员工临时请假，可替班人员中
- 有人缺少早产儿照护培训 → 硬性排除；
- 有人当天已承担两组夜间服务 → 需主管批准；
- 有人完全合适 → 普通偏好（推荐）。
"""
from __future__ import annotations

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
)

LONG = ("postnatal_care", dt(-30, 0), dt(60, 0))
PREEMIE = ("premature_infant_care", dt(-30, 0), dt(60, 0))


def _seed_leave_scenario(client):
    w_leave = add_worker(client, "王芳", "E001", [PREEMIE, LONG])       # 请假者
    w_no_skill = add_worker(client, "李梅", "E002", [LONG])             # 缺早产儿照护
    w_nights = add_worker(client, "张兰", "E003", [PREEMIE, LONG])      # 当天已 2 组夜班
    w_ok = add_worker(client, "赵敏", "E004", [PREEMIE, LONG])          # 正常候选

    fam_preemie = add_family(client, "陈家", ["premature_infant_care"],
                             notes="早产儿，需监测喂养")
    fam_plain1 = add_family(client, "林家")
    fam_plain2 = add_family(client, "周家")

    # 请假者王芳的班次：明天 23:00 – 后天 07:00（跨日夜间服务）
    target = add_shift(client, fam_preemie, dt(1, 23), dt(2, 7), worker_id=w_leave)

    # 张兰当天（day1）已有两组夜间服务，且与目标班次不冲突：
    #   00:00–06:00（跨夜间窗口）、19:00–20:30（跨 20:00 窗口）
    add_shift(client, fam_plain1, dt(1, 0), dt(1, 6), worker_id=w_nights)
    add_shift(client, fam_plain2, dt(1, 19), dt(1, 20, 30), worker_id=w_nights)

    return {
        "w_leave": w_leave, "w_no_skill": w_no_skill,
        "w_nights": w_nights, "w_ok": w_ok,
        "fam_preemie": fam_preemie, "target_shift_id": target["id"],
    }


def test_leave_report_groups_candidates_by_category(client):
    ids = _seed_leave_scenario(client)

    r = client.post("/api/leave", json={
        "worker_id": ids["w_leave"],
        "start_at": dt(1, 0).isoformat(),
        "end_at": dt(3, 0).isoformat(),
        "reason": "家中急事，请假两天",
    }, headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()

    assert data["worker"]["id"] == ids["w_leave"]
    assert len(data["affected_shifts"]) == 1
    affected = data["affected_shifts"][0]
    assert affected["shift"]["id"] == ids["target_shift_id"]
    assert affected["family"]["name"] == "陈家"

    cands = affected["candidates"]

    # 李梅：缺早产儿照护 → 硬性排除，并给出依据
    excluded = {c["worker_id"]: c for c in cands["excluded"]}
    assert ids["w_no_skill"] in excluded
    codes = [v["code"] for v in excluded[ids["w_no_skill"]]["hard_violations"]]
    assert "SKILL_MISSING" in codes
    assert any("早产儿照护" in v["detail"]
               for v in excluded[ids["w_no_skill"]]["hard_violations"])

    # 张兰：当天已 2 组夜班 → 需主管批准
    approval = {c["worker_id"]: c for c in cands["needs_approval"]}
    assert ids["w_nights"] in approval
    codes = [v["code"] for v in approval[ids["w_nights"]]["approval_reasons"]]
    assert "NIGHT_SHIFT_LIMIT" in codes
    assert approval[ids["w_nights"]]["night_shifts_that_day"] == 2

    # 赵敏：普通偏好（推荐），无硬性/批准项
    preferred = {c["worker_id"]: c for c in cands["preferred"]}
    assert ids["w_ok"] in preferred
    assert preferred[ids["w_ok"]]["hard_violations"] == []
    assert preferred[ids["w_ok"]]["approval_reasons"] == []

    # 请假者本人不出现在候选中
    all_ids = {c["worker_id"] for group in cands.values() for c in group}
    assert ids["w_leave"] not in all_ids


def test_candidates_endpoint_matches_leave_report(client):
    ids = _seed_leave_scenario(client)
    r = client.get(f"/api/shifts/{ids['target_shift_id']}/candidates",
                   headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["handover_required"] is False
    assert data["family"]["name"] == "陈家"
    excluded_ids = [c["worker_id"] for c in data["candidates"]["excluded"]]
    assert ids["w_no_skill"] in excluded_ids


def test_reassign_requires_approval_for_night_limit(client):
    ids = _seed_leave_scenario(client)
    sid = ids["target_shift_id"]

    # 未批准 → 409，返回需批准依据
    r = client.post(f"/api/shifts/{sid}/reassign",
                    json={"worker_id": ids["w_nights"]},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "APPROVAL_REQUIRED"
    codes = [v["code"] for v in body["evaluation"]["approval_reasons"]]
    assert "NIGHT_SHIFT_LIMIT" in codes

    # 主管批准后成功
    r = client.post(f"/api/shifts/{sid}/reassign",
                    json={"worker_id": ids["w_nights"], "approve": True,
                          "reason": "夜班人手不足，主管特批"},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["shift"]["worker_id"] == ids["w_nights"]


def test_reassign_hard_violation_rejected(client):
    ids = _seed_leave_scenario(client)
    r = client.post(f"/api/shifts/{ids['target_shift_id']}/reassign",
                    json={"worker_id": ids["w_no_skill"]},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 409, r.text
    body = r.json()
    assert body["code"] == "CONFLICT"
    codes = [v["code"] for v in body["evaluation"]["hard_violations"]]
    assert "SKILL_MISSING" in codes


def test_reassign_preferred_candidate_and_adjustment_view(client):
    ids = _seed_leave_scenario(client)
    sid = ids["target_shift_id"]

    r = client.post(f"/api/shifts/{sid}/reassign",
                    json={"worker_id": ids["w_ok"], "reason": "王芳请假"},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    adj_id = r.json()["adjustment_id"]

    # 经理查看一次调整：排除依据、连续服务时长、必要交接、受影响家庭
    r = client.get(f"/api/adjustments/{adj_id}", headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["adjustment"]["from_worker_id"] == ids["w_leave"]
    assert view["adjustment"]["to_worker_id"] == ids["w_ok"]
    assert view["affected_family"]["name"] == "陈家"
    assert view["evaluation"]["worker_id"] == ids["w_ok"]
    assert "continuous_service_hours" in view["evaluation"]
    assert "hard_violations" in view["evaluation"]
    assert view["handover_required"] is False
    assert view["handover"] is None
