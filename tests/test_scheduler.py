"""核心引擎规则测试：资质、技能、休息、连续服务、隔离、交接、到期扫描。"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.db import write_tx
from app.scheduler import SchedulingError, _HandoffRequired
from app.timeutil import iso


def _add_asg(conn, emp, fam, room, start, end, now):
    cur = conn.execute(
        """INSERT INTO assignments
           (employee_id, family_id, room_id, starts_at, ends_at, status, created_at)
           VALUES (?,?,?, ?,?, 'confirmed', ?)""",
        (emp, fam, room, iso(start), iso(end), iso(now)),
    )
    conn.commit()
    return cur.lastrowid


def _set_cert(conn, emp, name, vf, vu):
    conn.execute("DELETE FROM certifications WHERE employee_id=? AND name=?", (emp, name))
    conn.execute(
        "INSERT INTO certifications (employee_id, name, valid_from, valid_until) VALUES (?,?,?,?)",
        (emp, name, iso(vf), iso(vu)),
    )
    conn.commit()


def _candidate(adj, emp_id):
    return next(c for c in adj.candidates if c.employee_id == emp_id)


def _codes(cand, severity=None):
    return {i.code for i in cand.issues if severity is None or i.severity == severity}


# ---------- 资质与技能 ----------
def test_cert_missing_is_hard(ctx):
    """李姐缺少早产儿照护培训 → 硬性不符合。"""
    d = ctx["data"]
    adj = ctx["scheduler"].build_adjustment(d["target_assignment"], at=ctx["now"])
    b = _candidate(adj, d["employees"]["b"])
    assert not b.eligible
    assert "CERT_MISSING" in _codes(b, "hard")


def test_preference_skill_does_not_block(ctx):
    """缺少非必需技能只算普通偏好，不阻断资格。"""
    d = ctx["data"]
    adj = ctx["scheduler"].build_adjustment(d["target_assignment"], at=ctx["now"])
    c = _candidate(adj, d["employees"]["c"])  # 王姐：有早产儿资质、缺母乳指导
    assert c.eligible
    assert "SKILL_MISSING" in _codes(c, "preference")
    assert "母乳指导" in next(i.message for i in c.issues if i.code == "SKILL_MISSING")


def test_cert_expired_before_shift_is_hard(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    _set_cert(conn, d["employees"]["c"], "早产儿照护培训",
              ctx["now"] - timedelta(days=400), tonight - timedelta(hours=1))
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    c = _candidate(adj, d["employees"]["c"])
    assert not c.eligible
    assert "CERT_EXPIRED" in _codes(c, "hard")


def test_cert_expiring_midshift_is_hard(ctx):
    """重点：资质在班次进行中到期 → 硬性不符合（即使开始时仍有效）。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    # 班次 22:00–次日08:00；资质次日 02:00 到期
    _set_cert(conn, d["employees"]["c"], "早产儿照护培训",
              ctx["now"] - timedelta(days=30), tonight + timedelta(hours=4))
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    c = _candidate(adj, d["employees"]["c"])
    assert not c.eligible
    assert "CERT_MIDSHIFT" in _codes(c, "hard")


def test_cert_expiring_within_7_days_needs_approval(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    _set_cert(conn, d["employees"]["c"], "早产儿照护培训",
              ctx["now"] - timedelta(days=30), tonight + timedelta(hours=10, days=3))
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    c = _candidate(adj, d["employees"]["c"])
    assert c.eligible and c.needs_approval
    assert "CERT_EXPIRING_7D" in _codes(c, "approval")


# ---------- 休息间隔与连续服务（跨日服务） ----------
def test_cross_day_night_shift_and_rest_gap(ctx):
    """跨日夜班后紧接着的新班次：休息不足下限为硬性，介于下限与偏好间需批准。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    # 王姐今天 08:00–18:00 已服务一班；目标班今晚 22:00 开始 → 间隔 4h < 8h 硬性
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight - timedelta(hours=14), tonight - timedelta(hours=4), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert "REST_GAP" in _codes(cand, "hard")


def test_rest_short_requires_approval(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    # 上一班今天 09:00–13:00 在同一房间服务，新班今晚 22:00 开始 → 间隔 9h 介于 8 与 11
    _add_asg(conn, c, d["families"]["prem"], d["rooms"]["prem"],
             tonight - timedelta(hours=13), tonight - timedelta(hours=9), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert cand.eligible and cand.needs_approval
    assert "REST_SHORT" in _codes(cand, "approval")


def test_consecutive_cap_hard_and_hours_exposed(ctx):
    """连续两个跨日长班（10h+10h，间隔 9h）→ 连续 29h 超 26h 上限。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight - timedelta(hours=19), tonight - timedelta(hours=9), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert cand.consecutive_hours == pytest.approx(29.0, abs=0.01)
    assert "CONSECUTIVE_CAP" in _codes(cand, "hard")


def test_double_night_shift_requires_approval(ctx):
    """近 48 小时已承担两组夜间服务 → 主管批准项 NIGHT_LOAD。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    # 前晚 02:00–05:00、昨晚 02:00–05:00（均触及夜间时段，相互间隔 21h，
    # 距目标 22:00 班 17h，不触发硬性休息问题）
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight - timedelta(hours=44), tonight - timedelta(hours=41), ctx["now"])
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight - timedelta(hours=20), tonight - timedelta(hours=17), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert "NIGHT_LOAD" in _codes(cand, "approval")
    assert cand.eligible


# ---------- 时间段重叠：永不允许 ----------
def test_overlap_is_hard(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight + timedelta(hours=2), tonight + timedelta(hours=6), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert not cand.eligible
    assert "OVERLAP" in _codes(cand, "hard")


# ---------- 房间隔离 ----------
def test_strict_isolation_buffer_hard(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    # 目标是 strict 房间（早产宝宝家），王姐 6 小时后要到另一个 standard 房间
    _add_asg(conn, c, d["families"]["norm"], d["rooms"]["norm"],
             tonight + timedelta(hours=16), tonight + timedelta(hours=22), ctx["now"])
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, c)
    assert "ISOLATION_STRICT" in _codes(cand, "hard")


# ---------- 调班应用与交接 ----------
def test_reassign_future_shift_succeeds_and_supersedes(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    with write_tx(conn):
        adj = s.apply_reassignment(d["target_assignment"], d["employees"]["c"])
    assert adj.applied
    new = s.repo.get_assignment(adj.new_assignment_id)
    assert new["employee_id"] == d["employees"]["c"]
    old = s.repo.get_assignment(d["target_assignment"])
    assert old["status"] == "superseded"


def test_reassign_hard_candidate_rejected(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    with pytest.raises(SchedulingError):
        with write_tx(conn):
            s.apply_reassignment(d["target_assignment"], d["employees"]["b"])


def test_reassign_approval_requires_approver(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    _add_asg(conn, c, d["families"]["prem"], d["rooms"]["prem"],
             tonight - timedelta(hours=13), tonight - timedelta(hours=9), ctx["now"])
    with pytest.raises(SchedulingError):
        with write_tx(conn):
            s.apply_reassignment(d["target_assignment"], c)
    with write_tx(conn):
        adj = s.apply_reassignment(d["target_assignment"], c, approver_id=99)
    assert adj.applied


def test_in_progress_shift_cannot_be_silently_swapped(ctx):
    """已经开始的服务不允许静默换人：无交接确认 → 拒绝。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    # 目标班今晚才开始；改为评估一个已在进行中的时刻
    with pytest.raises(_HandoffRequired):
        with write_tx(conn):
            s.apply_reassignment(d["target_assignment"], d["employees"]["c"],
                                 at=tonight + timedelta(hours=2))
    # 班次未被改动
    assert s.repo.get_assignment(d["target_assignment"])["status"] == "confirmed"


def test_emergency_replacement_with_handoff_ack(ctx):
    """紧急接替：创建交接单 → 确认 → 调班成功。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    c = d["employees"]["c"]
    at = tonight + timedelta(hours=2)
    with write_tx(conn):
        hid = s.create_handoff(d["target_assignment"], c, at=at)
        s.acknowledge_handoff(hid, at=at + timedelta(minutes=5))
    with write_tx(conn):
        adj = s.apply_reassignment(d["target_assignment"], c, at=at)
    assert adj.applied and adj.required_handoff
    assert adj.handoff_id == hid


def test_handoff_double_ack_rejected(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    with write_tx(conn):
        hid = s.create_handoff(d["target_assignment"], d["employees"]["c"])
        s.acknowledge_handoff(hid)
    with pytest.raises(SchedulingError):
        with write_tx(conn):
            s.acknowledge_handoff(hid)


# ---------- 资质后来过期：只影响未开始班次 ----------
def test_expired_cert_scan_only_future_unstarted(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    a = d["employees"]["a"]
    # A 的早产儿资质此刻刚过期；其今晚的班尚未开始 → 必须列出重排
    _set_cert(conn, a, "早产儿照护培训",
              ctx["now"] - timedelta(days=400), ctx["now"] - timedelta(minutes=1))
    items = s.scan_expired_certifications(at=ctx["now"])
    flat = [asg.id for it in items for asg in it.affected_assignments]
    assert d["target_assignment"] in flat
    item = next(it for it in items if it.employee_id == a)
    assert item.certification == "早产儿照护培训"


def test_expired_cert_does_not_affect_started_shift(ctx):
    """班次已开始后资质才过期：进行中的安排不受影响，不列入重排。"""
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    tonight = d["tonight"]
    a = d["employees"]["a"]
    # 资质在班次中途过期（今晚 22:00 班、次日 02:00 到期），扫描时刻为次日 03:00（班仍在进行）
    _set_cert(conn, a, "早产儿照护培训",
              ctx["now"] - timedelta(days=400), tonight + timedelta(hours=4))
    items = s.scan_expired_certifications(
        at=tonight + timedelta(hours=5))
    flat = [asg.id for it in items for asg in it.affected_assignments]
    assert d["target_assignment"] not in flat


# ---------- 停用员工 ----------
def test_inactive_employee_hard(ctx):
    d = ctx["data"]
    s, conn = ctx["scheduler"], ctx["conn"]
    conn.execute("UPDATE employees SET active=0 WHERE id=?", (d["employees"]["c"],))
    conn.commit()
    adj = s.build_adjustment(d["target_assignment"], at=ctx["now"])
    cand = _candidate(adj, d["employees"]["c"])
    assert not cand.eligible
    assert "INACTIVE" in _codes(cand, "hard")


# ---------- 可解释性 / 受影响家庭 ----------
def test_adjustment_view_contains_explanations(ctx):
    d = ctx["data"]
    adj = ctx["scheduler"].build_adjustment(d["target_assignment"], at=ctx["now"])
    assert adj.family_name == "早产宝宝家"
    assert "34 周早产" in adj.care_summary
    rejected = _candidate(adj, d["employees"]["b"])
    assert any(i.severity == "hard" and i.code == "CERT_MISSING" for i in rejected.issues)
    # 每个候选都带连续服务时长字段
    for c in adj.candidates:
        assert isinstance(c.consecutive_hours, float)
