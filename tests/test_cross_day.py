"""跨日服务：夜班识别、跨日时间重叠、跨日休息间隔与连续服务时长。"""
from __future__ import annotations

from tests.conftest import (
    MANAGER_HEADERS,
    add_family,
    add_shift,
    add_worker,
    dt,
)


def _eval_of(client, shift_id, worker_id):
    r = client.get(f"/api/shifts/{shift_id}/candidates", headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    for group in r.json()["candidates"].values():
        for c in group:
            if c["worker_id"] == worker_id:
                return c
    raise AssertionError(f"候选中没有 worker {worker_id}")


def test_cross_day_shift_marked_as_night_service(client):
    w = add_worker(client, "王芳", "E001")
    fam = add_family(client, "陈家")
    night = add_shift(client, fam, dt(1, 22), dt(2, 6), worker_id=w)
    assert night["night_service"] is True
    assert night["duration_hours"] == 8.0

    day = add_shift(client, fam, dt(3, 10), dt(3, 14), worker_id=w)
    assert day["night_service"] is False


def test_cross_day_overlap_detected(client):
    """次日 05:00–09:00 的班次与 22:00–06:00 的跨日班次时间重叠。"""
    w_busy = add_worker(client, "王芳", "E001")
    w_free = add_worker(client, "李梅", "E002")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")

    add_shift(client, fam1, dt(1, 22), dt(2, 6), worker_id=w_busy)
    # 开放班次：次日 05:00–09:00，与王芳的跨日班次重叠 1 小时
    open_shift = add_shift(client, fam2, dt(2, 5), dt(2, 9))

    ev = _eval_of(client, open_shift["id"], w_busy)
    assert ev["category"] == "excluded"
    assert any(v["code"] == "TIME_OVERLAP" for v in ev["hard_violations"])

    ev_free = _eval_of(client, open_shift["id"], w_free)
    assert ev_free["category"] == "preferred"


def test_cross_day_rest_interval(client):
    """跨日班次结束于 06:00，下一班 07:30 开始 → 休息 1.5 小时，硬性排除。"""
    w = add_worker(client, "王芳", "E001")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")

    add_shift(client, fam1, dt(1, 22), dt(2, 6), worker_id=w)
    early = add_shift(client, fam2, dt(2, 7, 30), dt(2, 12))

    ev = _eval_of(client, early["id"], w)
    assert ev["category"] == "excluded"
    assert any(v["code"] == "REST_INTERVAL" for v in ev["hard_violations"])
    assert ev["rest_before_hours"] == 1.5


def test_continuous_service_across_midnight_needs_approval(client):
    """20:00–24:00 与 02:30–11:30 间隔 2.5 小时 → 连续服务 13 小时，需主管批准。"""
    w = add_worker(client, "王芳", "E001")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")

    add_shift(client, fam1, dt(1, 20), dt(2, 0), worker_id=w)
    target = add_shift(client, fam2, dt(2, 2, 30), dt(2, 11, 30))

    ev = _eval_of(client, target["id"], w)
    assert ev["category"] == "needs_approval"
    assert ev["continuous_service_hours"] == 13.0
    assert any(v["code"] == "CONTINUOUS_SERVICE_LONG"
               for v in ev["approval_reasons"])


def test_continuous_service_hard_limit_across_midnight(client):
    """18:00–次日02:00（8h）接 03:00–12:00（9h）→ 连续 17 小时，硬性排除。"""
    w = add_worker(client, "王芳", "E001")
    fam1 = add_family(client, "陈家")
    fam2 = add_family(client, "林家")

    add_shift(client, fam1, dt(1, 18), dt(2, 2), worker_id=w)
    target = add_shift(client, fam2, dt(2, 3), dt(2, 12))

    ev = _eval_of(client, target["id"], w)
    assert ev["category"] == "excluded"
    assert ev["continuous_service_hours"] == 17.0
    assert any(v["code"] == "CONTINUOUS_SERVICE_LIMIT"
               for v in ev["hard_violations"])


def test_reassign_onto_cross_day_shift(client):
    """跨日班次可以正常调班给空闲月嫂。"""
    w_leave = add_worker(client, "王芳", "E001")
    w_ok = add_worker(client, "赵敏", "E002")
    fam = add_family(client, "陈家")

    shift = add_shift(client, fam, dt(1, 22), dt(2, 6), worker_id=w_leave)
    r = client.post(f"/api/shifts/{shift['id']}/reassign",
                    json={"worker_id": w_ok}, headers=MANAGER_HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["shift"]["worker_id"] == w_ok
    assert r.json()["shift"]["night_service"] is True
