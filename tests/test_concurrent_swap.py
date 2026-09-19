"""并发调班：同一时间段绝不允许重复承担服务。"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
    hours_from_now,
    transition,
)


def _reassign(app, shift_id, worker_id):
    """每个线程使用独立 TestClient，模拟两个经理同时操作。"""
    with TestClient(app) as c:
        return c.post(f"/api/shifts/{shift_id}/reassign",
                      json={"worker_id": worker_id},
                      headers=MANAGER_HEADERS)


def test_concurrent_reassign_same_worker_no_double_booking(app, client):
    """两个重叠班次同时调给同一名月嫂：只有一个成功。"""
    w = add_worker(client, "赵敏", "E001")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")
    s1 = add_shift(client, fam1, dt(1, 10), dt(1, 14))
    s2 = add_shift(client, fam2, dt(1, 12), dt(1, 16))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(
            lambda sid: _reassign(app, sid, w), [s1["id"], s2["id"]]))

    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409], [r.text for r in results]

    # 失败方返回时间重叠的硬性依据
    loser = next(r for r in results if r.status_code == 409)
    violations = loser.json()["evaluation"]["hard_violations"]
    assert any(v["code"] == "TIME_OVERLAP" for v in violations)

    # 终态：该月嫂在重叠时段只有一个班次
    r = client.get("/api/my/schedule", params={"worker_id": w},
                   headers=MANAGER_HEADERS)
    assert r.status_code == 200
    assert len(r.json()["shifts"]) == 1


def test_concurrent_reassign_non_overlapping_both_succeed(app, client):
    """不重叠的班次并发调给同一名月嫂：两个都成功（不错误拒绝）。"""
    w = add_worker(client, "赵敏", "E001")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")
    # 间隔 24 小时以上，休息充足
    s1 = add_shift(client, fam1, dt(1, 8), dt(1, 12))
    s2 = add_shift(client, fam2, dt(3, 8), dt(3, 12))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(
            lambda sid: _reassign(app, sid, w), [s1["id"], s2["id"]]))

    assert sorted(r.status_code for r in results) == [200, 200]


def test_concurrent_handover_confirm_only_once(app, client):
    """同一交接单被并发确认：只成功一次。"""
    w_from = add_worker(client, "王芳", "E001")
    w_to = add_worker(client, "赵敏", "E002")
    fam = add_family(client, "陈家")
    shift = add_shift(client, fam, hours_from_now(-1), hours_from_now(7),
                      worker_id=w_from)
    transition(client, shift["id"], "start")

    r = client.post(f"/api/shifts/{shift['id']}/emergency-replacement",
                    json={"worker_id": w_to, "reason": "突发不适"},
                    headers=MANAGER_HEADERS)
    assert r.status_code == 201, r.text
    handover_id = r.json()["handover"]["id"]

    def _confirm():
        with TestClient(app) as c:
            return c.post(f"/api/handovers/{handover_id}/confirm",
                          headers=MANAGER_HEADERS)

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(lambda _: _confirm(), range(2)))

    assert sorted(r.status_code for r in results) == [200, 409]
