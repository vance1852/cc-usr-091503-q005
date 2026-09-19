"""紧急接替与交接确认：已开始的服务不允许静默换人。"""
from __future__ import annotations

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
    hours_from_now,
    transition,
)

SKILL = "premature_infant_care"


def _seed_in_progress(client, required_skills=()):
    """构造一个进行中的班次：1 小时前开始，7 小时后结束。"""
    w_from = add_worker(client, "王芳", "E001",
                        [(s, dt(-30, 0), dt(60, 0)) for s in required_skills])
    fam = add_family(client, "陈家", required_skills, notes="早产儿，需监测喂养")
    shift = add_shift(client, fam, hours_from_now(-1), hours_from_now(7),
                      worker_id=w_from)
    transition(client, shift["id"], "start")
    return w_from, fam, shift


def test_in_progress_shift_forbids_silent_reassign(client):
    w_from, _, shift = _seed_in_progress(client)
    w_new = add_worker(client, "赵敏", "E002")

    r = client.post(f"/api/shifts/{shift['id']}/reassign",
                    json={"worker_id": w_new}, headers=MANAGER_HEADERS)
    assert r.status_code == 409, r.text
    assert r.json()["code"] == "SILENT_SWAP_FORBIDDEN"

    # 班次人员未变
    r = client.get(f"/api/shifts/{shift['id']}", headers=MANAGER_HEADERS)
    assert r.json()["worker_id"] == w_from
    assert r.json()["status"] == "in_progress"


def test_emergency_replacement_requires_handover_confirmation(client):
    w_from, _, shift = _seed_in_progress(client)
    w_new = add_worker(client, "赵敏", "E002")

    # 发起紧急接替 → 待确认交接单
    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_new, "reason": "王芳突发不适"},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 201, r.text
    handover = r.json()["handover"]
    assert handover["status"] == "pending"
    assert handover["from_worker_id"] == w_from
    assert handover["to_worker_id"] == w_new

    # 确认前：班次仍属原月嫂
    r = client.get(f"/api/shifts/{shift['id']}", headers=MANAGER_HEADERS)
    assert r.json()["worker_id"] == w_from

    # 同一班次不能重复发起待确认交接
    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_new}, headers=MANAGER_HEADERS)
    assert r.status_code == 409

    # 交接确认 → 原班次截断完成，新班次由接替人承担
    r = client.post(f"/api/handovers/{handover['id']}/confirm",
                    headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["handover"]["status"] == "confirmed"
    new_shift = data["new_shift"]
    assert new_shift["worker_id"] == w_new
    assert new_shift["status"] == "in_progress"

    r = client.get(f"/api/shifts/{shift['id']}", headers=MANAGER_HEADERS)
    assert r.json()["status"] == "completed"

    # 重复确认 → 409
    r = client.post(f"/api/handovers/{handover['id']}/confirm",
                    headers=MANAGER_HEADERS)
    assert r.status_code == 409

    # 调整回放包含交接信息
    r = client.get(f"/api/adjustments/{data['adjustment_id']}",
                   headers=MANAGER_HEADERS)
    view = r.json()
    assert view["adjustment"]["kind"] == "emergency_handover"
    assert view["handover"]["id"] == handover["id"]
    assert view["affected_family"]["name"] == "陈家"
    assert view["evaluation"]["worker_id"] == w_new


def test_emergency_replacement_rejects_unqualified_worker(client):
    """接替人缺少必需技能 → 紧急接替被拒绝。"""
    _, _, shift = _seed_in_progress(client, required_skills=[SKILL])
    w_unqualified = add_worker(client, "李梅", "E002")  # 无早产儿照护资质

    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_unqualified}, headers=MANAGER_HEADERS)
    assert r.status_code == 409, r.text
    codes = [v["code"] for v in r.json()["evaluation"]["hard_violations"]]
    assert "SKILL_MISSING" in codes


def test_emergency_replacement_rejects_conflicting_worker(client):
    """接替人剩余时段内有其他班次 → 不允许同时段重复承担。"""
    _, fam2, shift = _seed_in_progress(client)
    w_busy = add_worker(client, "张兰", "E002")
    # 张兰 2 小时后另有一班，与接替窗口（现在 – 7 小时后）重叠
    add_shift(client, fam2, hours_from_now(2), hours_from_now(4), worker_id=w_busy)

    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_busy}, headers=MANAGER_HEADERS)
    assert r.status_code == 409, r.text
    codes = [v["code"] for v in r.json()["evaluation"]["hard_violations"]]
    assert "TIME_OVERLAP" in codes


def test_not_started_shift_cannot_use_emergency_replacement(client):
    w = add_worker(client, "王芳", "E001")
    w_new = add_worker(client, "赵敏", "E002")
    fam = add_family(client, "陈家")
    shift = add_shift(client, fam, dt(1, 8), dt(1, 16), worker_id=w)

    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_new}, headers=MANAGER_HEADERS)
    assert r.status_code == 409
    assert "未开始" in r.json()["detail"]
