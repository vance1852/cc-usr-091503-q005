"""业务编排：调班、请假评估、紧急接替、交接确认、资质过期扫描。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from .constraints import evaluate_candidate, is_night_shift, skill_label
from .models import (
    SHIFT_ACTIVE,
    SHIFT_NOT_STARTED,
    Adjustment,
    Family,
    Handover,
    Qualification,
    Shift,
    Worker,
)
from .utils import fmt, to_api, to_store, utcnow


# ---- 异常：由接口层映射为 HTTP 状态码 ----
class ServiceError(Exception):
    status_code = 400
    code = "BAD_REQUEST"

    def __init__(self, message: str, payload: dict | None = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


class NotFoundError(ServiceError):
    status_code = 404
    code = "NOT_FOUND"


class ConflictError(ServiceError):
    status_code = 409
    code = "CONFLICT"


class ApprovalRequired(ConflictError):
    code = "APPROVAL_REQUIRED"


class StateError(ConflictError):
    code = "INVALID_STATE"


# ---- 序列化 ----
def qualification_dict(q: Qualification) -> dict:
    return {
        "id": q.id,
        "worker_id": q.worker_id,
        "skill": q.skill,
        "skill_label": skill_label(q.skill),
        "valid_from": to_api(q.valid_from),
        "valid_until": to_api(q.valid_until),
    }


def worker_dict(w: Worker) -> dict:
    return {
        "id": w.id,
        "name": w.name,
        "employee_no": w.employee_no,
        "active": w.active,
        "qualifications": [qualification_dict(q) for q in w.qualifications],
    }


def family_dict(f: Family) -> dict:
    return {
        "id": f.id,
        "name": f.name,
        "room": f.room,
        "isolation_required": f.isolation_required,
        "required_skills": list(f.required_skills or []),
        "care_notes": f.care_notes,
    }


def shift_dict(s: Shift) -> dict:
    return {
        "id": s.id,
        "family_id": s.family_id,
        "family_name": s.family.name if s.family else None,
        "worker_id": s.worker_id,
        "worker_name": s.worker.name if s.worker else None,
        "start_at": to_api(s.start_at),
        "end_at": to_api(s.end_at),
        "duration_hours": round((s.end_at - s.start_at).total_seconds() / 3600, 2),
        "status": s.status,
        "night_service": s.night_service,
        "needs_reassignment": s.needs_reassignment,
    }


def handover_dict(h: Handover) -> dict:
    return {
        "id": h.id,
        "shift_id": h.shift_id,
        "from_worker_id": h.from_worker_id,
        "to_worker_id": h.to_worker_id,
        "reason": h.reason,
        "status": h.status,
        "handover_at": to_api(h.handover_at),
        "created_at": to_api(h.created_at),
        "confirmed_at": to_api(h.confirmed_at),
    }


def adjustment_dict(a: Adjustment) -> dict:
    return {
        "id": a.id,
        "shift_id": a.shift_id,
        "kind": a.kind,
        "from_worker_id": a.from_worker_id,
        "to_worker_id": a.to_worker_id,
        "actor": a.actor,
        "approved": a.approved,
        "handover_id": a.handover_id,
        "reason": (a.detail or {}).get("reason", ""),
        "created_at": to_api(a.created_at),
    }


# ---- 基础 ----
def get_worker_or_404(session: Session, worker_id: int) -> Worker:
    w = session.get(Worker, worker_id)
    if w is None:
        raise NotFoundError(f"月嫂 #{worker_id} 不存在")
    return w


def get_shift_or_404(session: Session, shift_id: int) -> Shift:
    s = session.get(Shift, shift_id)
    if s is None:
        raise NotFoundError(f"班次 #{shift_id} 不存在")
    return s


def _check_eval_or_raise(ev, approve: bool) -> None:
    """硬性不符合 → 409；需批准而未批准 → 409。"""
    if ev.hard:
        raise ConflictError("候选人存在硬性不符合，无法安排",
                            payload={"evaluation": ev.to_dict()})
    if ev.approvals and not approve:
        raise ApprovalRequired("该安排触及负荷限制，需要主管批准后重试（approve=true）",
                               payload={"evaluation": ev.to_dict()})


# ---- 班次创建与状态机 ----
def create_shift(session: Session, data, actor: str) -> tuple[Shift, object | None]:
    family = session.get(Family, data.family_id)
    if family is None:
        raise NotFoundError(f"家庭 #{data.family_id} 不存在")
    start, end = to_store(data.start_at), to_store(data.end_at)
    if end <= start:
        raise ServiceError("班次结束时间必须晚于开始时间")
    shift = Shift(
        family_id=family.id,
        start_at=start,
        end_at=end,
        night_service=is_night_shift(start, end),
    )
    session.add(shift)
    session.flush()  # 让 relationship 可加载，便于评估

    ev = None
    if data.worker_id is not None:
        worker = get_worker_or_404(session, data.worker_id)
        ev = evaluate_candidate(session, shift, worker)
        _check_eval_or_raise(ev, data.approve)
        shift.worker_id = worker.id
    session.commit()
    return shift, ev


_TRANSITIONS = {
    "confirm": (("scheduled",), "confirmed"),
    "start": (("scheduled", "confirmed"), "in_progress"),
    "complete": (("in_progress",), "completed"),
    "cancel": (("scheduled", "confirmed"), "cancelled"),
}


def transition_shift(session: Session, shift_id: int, action: str, actor: str) -> Shift:
    shift = get_shift_or_404(session, shift_id)
    allowed_from, target = _TRANSITIONS[action]
    if shift.status not in allowed_from:
        raise StateError(f"班次当前状态为 {shift.status}，不能执行 {action}")
    if action in ("confirm", "start") and shift.worker_id is None:
        raise StateError("班次尚未指派服务人员")
    shift.status = target
    session.commit()
    return shift


# ---- 候选评估 ----
def evaluate_all(session: Session, shift: Shift) -> dict:
    """对全部在岗月嫂评估，按三档分组返回。"""
    now = utcnow()
    # 已开始的服务只评估剩余时段（紧急接替场景）
    window_start = now if shift.status == "in_progress" else None
    workers = session.query(Worker).filter(Worker.active.is_(True)).all()
    groups: dict[str, list] = {"preferred": [], "needs_approval": [], "excluded": []}
    for w in workers:
        if w.id == shift.worker_id:
            continue  # 候选人是替班者，不含当前任职者
        ev = evaluate_candidate(session, shift, w, window_start=window_start)
        groups[ev.category].append(ev.to_dict())
    groups["preferred"].sort(key=lambda e: -e["score"])
    groups["needs_approval"].sort(key=lambda e: -e["score"])
    groups["excluded"].sort(key=lambda e: (-len(e["hard_violations"]), e["worker_id"]))
    return groups


def candidates_for_shift(session: Session, shift_id: int) -> dict:
    shift = get_shift_or_404(session, shift_id)
    in_progress = shift.status == "in_progress"
    return {
        "shift": shift_dict(shift),
        "family": family_dict(shift.family),
        "handover_required": in_progress,
        "evaluation_window": {
            "start_at": to_api(utcnow()) if in_progress else to_api(shift.start_at),
            "end_at": to_api(shift.end_at),
            "note": "服务进行中，仅评估剩余时段" if in_progress else "评估整个班次时段",
        },
        "candidates": evaluate_all(session, shift),
    }


# ---- 请假：找出受影响班次并给出候选 ----
def report_leave(session: Session, data, actor: str) -> dict:
    worker = get_worker_or_404(session, data.worker_id)
    start, end = to_store(data.start_at), to_store(data.end_at)
    if end <= start:
        raise ServiceError("请假结束时间必须晚于开始时间")

    upcoming = (
        session.query(Shift)
        .filter(Shift.worker_id == worker.id,
                Shift.status.in_(SHIFT_NOT_STARTED),
                Shift.start_at < end, Shift.end_at > start)
        .order_by(Shift.start_at)
        .all()
    )
    in_progress = (
        session.query(Shift)
        .filter(Shift.worker_id == worker.id,
                Shift.status == "in_progress",
                Shift.start_at < end, Shift.end_at > start)
        .order_by(Shift.start_at)
        .all()
    )
    return {
        "worker": worker_dict(worker),
        "reason": data.reason,
        "window": {"start_at": to_api(start), "end_at": to_api(end)},
        "affected_shifts": [
            {
                "shift": shift_dict(s),
                "family": family_dict(s.family),
                "candidates": evaluate_all(session, s),
            }
            for s in upcoming
        ],
        "in_progress_shifts": [
            {**shift_dict(s),
             "hint": "服务已开始，禁止静默换人；如需接替请走紧急接替并完成交接确认"}
            for s in in_progress
        ],
    }


# ---- 调班（仅限未开始的班次）----
def reassign_shift(session: Session, shift_id: int, data, actor: str):
    shift = get_shift_or_404(session, shift_id)
    if shift.status == "in_progress":
        raise StateError(
            "服务已开始，禁止静默换人；请发起紧急接替并完成交接确认",
            payload={"code": "SILENT_SWAP_FORBIDDEN", "shift_id": shift.id},
        )
    if shift.status not in SHIFT_NOT_STARTED:
        raise StateError(f"班次当前状态为 {shift.status}，不可调班")
    worker = get_worker_or_404(session, data.worker_id)
    if worker.id == shift.worker_id:
        raise ConflictError("该班次已由此月嫂承担")

    ev = evaluate_candidate(session, shift, worker)
    _check_eval_or_raise(ev, data.approve)

    old_worker_id = shift.worker_id
    shift.worker_id = worker.id
    shift.needs_reassignment = False
    adj = Adjustment(
        shift_id=shift.id,
        kind="reassign",
        from_worker_id=old_worker_id,
        to_worker_id=worker.id,
        actor=actor,
        approved=bool(data.approve and ev.approvals),
        detail={"reason": data.reason, "evaluation": ev.to_dict()},
    )
    session.add(adj)
    session.commit()
    return shift, adj, ev


# ---- 紧急接替与交接确认 ----
def emergency_replace(session: Session, shift_id: int, data, actor: str):
    shift = get_shift_or_404(session, shift_id)
    if shift.status != "in_progress":
        raise StateError("紧急接替仅适用于服务中的班次；未开始的班次请直接调班")
    worker = get_worker_or_404(session, data.worker_id)
    if worker.id == shift.worker_id:
        raise ConflictError("接替人不能是当前服务人员")
    existing = (
        session.query(Handover)
        .filter(Handover.shift_id == shift.id, Handover.status == "pending")
        .first()
    )
    if existing:
        raise ConflictError("该班次已有待确认的交接",
                            payload={"handover_id": existing.id})

    now = utcnow()
    # 只评估剩余时段 [now, end)
    ev = evaluate_candidate(session, shift, worker, window_start=now)
    _check_eval_or_raise(ev, data.approve)

    h = Handover(
        shift_id=shift.id,
        from_worker_id=shift.worker_id,
        to_worker_id=worker.id,
        reason=data.reason,
        handover_at=now,
    )
    session.add(h)
    session.commit()
    return h, ev


def confirm_handover(session: Session, handover_id: int, actor: str):
    h = session.get(Handover, handover_id)
    if h is None:
        raise NotFoundError(f"交接单 #{handover_id} 不存在")
    if h.status != "pending":
        raise StateError(f"交接单状态为 {h.status}，不可重复确认")
    shift = h.shift
    if not (shift.start_at < h.handover_at < shift.end_at):
        raise StateError("交接时刻不在班次时段内")
    worker = get_worker_or_404(session, h.to_worker_id)

    now = utcnow()
    old_end = shift.end_at
    # 原班次截断：原月嫂服务到交接时刻为止
    shift.end_at = h.handover_at
    shift.status = "completed"
    # 新班次：接替人从交接时刻服务到原结束时刻
    new_shift = Shift(
        family_id=shift.family_id,
        worker_id=worker.id,
        start_at=h.handover_at,
        end_at=old_end,
        status="in_progress" if h.handover_at <= now else "scheduled",
        night_service=is_night_shift(h.handover_at, old_end),
    )
    session.add(new_shift)
    session.flush()
    # 事务内再次校验：任何情况下都不允许同时段重复承担
    ev = evaluate_candidate(session, new_shift, worker)
    if ev.hard:
        raise ConflictError("交接确认时校验失败，未执行换人",
                            payload={"evaluation": ev.to_dict()})

    h.status = "confirmed"
    h.confirmed_at = now
    adj = Adjustment(
        shift_id=new_shift.id,
        kind="emergency_handover",
        from_worker_id=h.from_worker_id,
        to_worker_id=h.to_worker_id,
        actor=actor,
        handover_id=h.id,
        detail={"reason": h.reason, "original_shift_id": shift.id,
                "evaluation": ev.to_dict()},
    )
    session.add(adj)
    session.commit()
    return h, new_shift, adj


def reject_handover(session: Session, handover_id: int, actor: str) -> Handover:
    h = session.get(Handover, handover_id)
    if h is None:
        raise NotFoundError(f"交接单 #{handover_id} 不存在")
    if h.status != "pending":
        raise StateError(f"交接单状态为 {h.status}，不可驳回")
    h.status = "rejected"
    session.commit()
    return h


# ---- 资质过期：只影响尚未开始的安排 ----
def expire_qualification(session: Session, qual_id: int, effective_at, actor: str):
    q = session.get(Qualification, qual_id)
    if q is None:
        raise NotFoundError(f"资质 #{qual_id} 不存在")
    now = utcnow()
    q.valid_until = to_store(effective_at) if effective_at else now

    affected = []
    upcoming = (
        session.query(Shift)
        .filter(Shift.worker_id == q.worker_id,
                Shift.status.in_(SHIFT_NOT_STARTED),
                Shift.start_at > now)
        .all()
    )
    for s in upcoming:
        if q.skill not in (s.family.required_skills or []):
            continue
        if _has_covering_qual(q.worker, q.skill, s.start_at, s.end_at):
            continue  # 另有同技能资质仍可覆盖
        s.needs_reassignment = True
        affected.append({
            "shift_id": s.id,
            "family_id": s.family_id,
            "family_name": s.family.name,
            "start_at": to_api(s.start_at),
            "end_at": to_api(s.end_at),
            "missing_skill": q.skill,
            "reason": f"「{skill_label(q.skill)}」资质于 {fmt(q.valid_until)} 到期，"
                      f"无法覆盖班次（{fmt(s.start_at)}–{fmt(s.end_at)}）",
        })
    session.commit()
    return q, affected


def _has_covering_qual(worker: Worker, skill: str, start: datetime, end: datetime) -> bool:
    return any(
        q.skill == skill and q.valid_from <= start and q.valid_until >= end
        for q in worker.qualifications
    )


def reassignments_needed(session: Session) -> list[dict]:
    """列出所有因资质失效等待重排的班次及受影响家庭。"""
    shifts = (
        session.query(Shift)
        .filter(Shift.needs_reassignment.is_(True),
                Shift.status.in_(SHIFT_NOT_STARTED))
        .order_by(Shift.start_at)
        .all()
    )
    result = []
    for s in shifts:
        reasons = []
        if s.worker is not None:
            ev = evaluate_candidate(session, s, s.worker)
            reasons = [v.to_dict() for v in ev.hard]
        result.append({
            "shift": shift_dict(s),
            "family": family_dict(s.family),
            "current_worker_id": s.worker_id,
            "reasons": reasons,
        })
    return result


# ---- 调整回放（经理视图）----
def adjustment_view(session: Session, adjustment_id: int) -> dict:
    adj = session.get(Adjustment, adjustment_id)
    if adj is None:
        raise NotFoundError(f"调整记录 #{adjustment_id} 不存在")
    shift = get_shift_or_404(session, adj.shift_id)
    handover = session.get(Handover, adj.handover_id) if adj.handover_id else None
    return {
        "adjustment": adjustment_dict(adj),
        "shift": shift_dict(shift),
        "affected_family": family_dict(shift.family),
        # 候选人评估快照：排除依据、连续服务时长等
        "evaluation": (adj.detail or {}).get("evaluation"),
        # 必要交接（紧急接替时关联的交接单）
        "handover": handover_dict(handover) if handover else None,
        "handover_required": shift.status == "in_progress",
    }
