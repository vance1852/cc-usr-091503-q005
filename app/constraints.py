"""负荷编排约束引擎。

每条规则归入三档，并产出可解释依据：
- 硬性不符合（hard）：直接排除，不可排班；
- 需要主管批准（approval）：可以排，但必须主管显式批准；
- 普通偏好（preference）：只影响候选排序。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .models import SHIFT_ACTIVE, Shift, Worker
from .utils import fmt

# ---- 规则参数（按机构制度调整）----
HARD_MIN_REST_HOURS = 2.0         # 相邻班次最小休息间隔（低于 → 硬性排除）
PREFERRED_REST_HOURS = 8.0        # 建议休息间隔（低于 → 需主管批准）
SOFT_MAX_CONTINUOUS_HOURS = 12.0  # 连续服务软上限（超过 → 需主管批准）
HARD_MAX_CONTINUOUS_HOURS = 16.0  # 连续服务硬上限（超过 → 硬性排除）
MAX_NIGHT_SHIFTS_PER_DAY = 2      # 同一自然日夜间服务组数上限（达到 → 需主管批准）
CONTINUITY_GAP_HOURS = 4.0        # 相邻班次间隔小于该值视为连续服务
ISOLATION_BUFFER_HOURS = 4.0      # 隔离家庭班次前后缓冲（小时）
NIGHT_START_HOUR = 20             # 夜间窗口 20:00 – 次日 08:00
NIGHT_END_HOUR = 8

SKILL_LABELS = {
    "premature_infant_care": "早产儿照护",
    "postnatal_care": "产后护理",
    "neonatal_care": "新生儿护理",
    "lactation_support": "通乳催乳",
}


def skill_label(skill: str) -> str:
    return SKILL_LABELS.get(skill, skill)


@dataclass
class Violation:
    code: str
    detail: str

    def to_dict(self) -> dict:
        return {"code": self.code, "detail": self.detail}


@dataclass
class CandidateEval:
    """一名月嫂对某个班次的评估结果（可解释）。"""

    worker_id: int
    worker_name: str
    hard: list[Violation] = field(default_factory=list)
    approvals: list[Violation] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    continuous_hours: float = 0.0
    rest_before_hours: float | None = None
    rest_after_hours: float | None = None
    night_shifts_that_day: int = 0
    score: float = 100.0

    @property
    def category(self) -> str:
        if self.hard:
            return "excluded"
        if self.approvals:
            return "needs_approval"
        return "preferred"

    def to_dict(self) -> dict:
        return {
            "worker_id": self.worker_id,
            "worker_name": self.worker_name,
            "category": self.category,
            "hard_violations": [v.to_dict() for v in self.hard],
            "approval_reasons": [v.to_dict() for v in self.approvals],
            "preference_notes": list(self.notes),
            "continuous_service_hours": self.continuous_hours,
            "rest_before_hours": self.rest_before_hours,
            "rest_after_hours": self.rest_after_hours,
            "night_shifts_that_day": self.night_shifts_that_day,
            "score": round(self.score, 1),
        }


def is_night_shift(start: datetime, end: datetime) -> bool:
    """班次与任意一天 20:00–次日 08:00 的夜间窗口重叠即为夜间服务（支持跨日）。"""
    day = start.date() - timedelta(days=1)
    while day <= end.date():
        night_start = datetime(day.year, day.month, day.day, NIGHT_START_HOUR)
        night_end = night_start + timedelta(hours=(24 - NIGHT_START_HOUR) + NIGHT_END_HOUR)
        if night_start < end and night_end > start:
            return True
        day += timedelta(days=1)
    return False


def _active_shifts(session: Session, worker_id: int, exclude_id: int | None = None) -> list[Shift]:
    q = session.query(Shift).filter(
        Shift.worker_id == worker_id, Shift.status.in_(SHIFT_ACTIVE)
    )
    if exclude_id is not None:
        q = q.filter(Shift.id != exclude_id)
    return q.all()


def evaluate_candidate(
    session: Session,
    shift: Shift,
    worker: Worker,
    window_start: datetime | None = None,
    window_end: datetime | None = None,
) -> CandidateEval:
    """评估一名月嫂对某个班次（或其剩余窗口）的适配度，返回可解释结果。

    window_start/window_end 用于紧急接替：只评估 [交接时刻, 班次结束] 的剩余时段。
    """
    start = window_start or shift.start_at
    end = window_end or shift.end_at
    family = shift.family
    ev = CandidateEval(worker_id=worker.id, worker_name=worker.name)

    if not worker.active:
        ev.hard.append(Violation("WORKER_INACTIVE", "员工已停用"))
        return ev

    required = list(family.required_skills or [])

    # 1) 专项技能与资质有效期（须覆盖整个服务窗口）
    for skill in required:
        quals = [q for q in worker.qualifications if q.skill == skill]
        if not quals:
            ev.hard.append(Violation("SKILL_MISSING", f"缺少「{skill_label(skill)}」专项资质"))
            continue
        if any(q.valid_from <= start and q.valid_until >= end for q in quals):
            continue
        mid = [q for q in quals if q.valid_from <= start and start < q.valid_until < end]
        if mid:
            ev.hard.append(Violation(
                "QUALIFICATION_EXPIRES_MID_SHIFT",
                f"「{skill_label(skill)}」资质将于 {fmt(mid[0].valid_until)} 到期，"
                f"早于班次结束 {fmt(end)}",
            ))
        else:
            ev.hard.append(Violation(
                "QUALIFICATION_NOT_VALID",
                f"「{skill_label(skill)}」资质有效期不覆盖班次时段",
            ))

    others = _active_shifts(session, worker.id, exclude_id=shift.id)

    # 2) 同一时间段重复承担
    for s in others:
        if s.start_at < end and s.end_at > start:
            ev.hard.append(Violation(
                "TIME_OVERLAP",
                f"与已排班次#{s.id}（{fmt(s.start_at)}–{fmt(s.end_at)}）时间重叠",
            ))

    # 3) 房间隔离要求：隔离家庭班次前后缓冲期内不得安排其他家庭
    buffer = timedelta(hours=ISOLATION_BUFFER_HOURS)
    reported: set[int] = set()
    for s in others:
        fam = s.family
        if fam.isolation_required and s.start_at - buffer < end and s.end_at + buffer > start:
            reported.add(s.id)
            ev.hard.append(Violation(
                "ISOLATION_CONFLICT",
                f"家庭「{fam.name}」要求房间隔离：班次#{s.id}（{fmt(s.start_at)}–{fmt(s.end_at)}）"
                f"前后 {ISOLATION_BUFFER_HOURS:g} 小时内不得安排其他家庭",
            ))
    if family.isolation_required:
        for s in others:
            if s.id in reported:
                continue
            if start - buffer < s.end_at and end + buffer > s.start_at:
                ev.hard.append(Violation(
                    "ISOLATION_CONFLICT",
                    f"本班次家庭要求房间隔离，与班次#{s.id}（{fmt(s.start_at)}–{fmt(s.end_at)}）"
                    f"间隔不足 {ISOLATION_BUFFER_HOURS:g} 小时",
                ))

    # 4) 休息间隔
    prev_shifts = [s for s in others if s.end_at <= start]
    next_shifts = [s for s in others if s.start_at >= end]
    if prev_shifts:
        prev = max(prev_shifts, key=lambda s: s.end_at)
        ev.rest_before_hours = round((start - prev.end_at).total_seconds() / 3600, 2)
    if next_shifts:
        nxt = min(next_shifts, key=lambda s: s.start_at)
        ev.rest_after_hours = round((nxt.start_at - end).total_seconds() / 3600, 2)
    for label, rest in (("上一班", ev.rest_before_hours), ("下一班", ev.rest_after_hours)):
        if rest is None:
            continue
        if rest < HARD_MIN_REST_HOURS:
            ev.hard.append(Violation(
                "REST_INTERVAL",
                f"距{label}仅休息 {rest:g} 小时，低于硬性下限 {HARD_MIN_REST_HOURS:g} 小时",
            ))
        elif rest < PREFERRED_REST_HOURS:
            ev.approvals.append(Violation(
                "REST_INTERVAL_SHORT",
                f"距{label}休息 {rest:g} 小时，低于建议值 {PREFERRED_REST_HOURS:g} 小时",
            ))

    # 5) 连续服务时长：间隔小于 CONTINUITY_GAP_HOURS 的相邻班次视为连续
    chain: list[Shift] = []
    cur_start, cur_end = start, end
    remaining = list(others)
    changed = True
    while changed:
        changed = False
        for s in list(remaining):
            touches_before = s.end_at <= cur_start and (cur_start - s.end_at) < timedelta(
                hours=CONTINUITY_GAP_HOURS)
            touches_after = s.start_at >= cur_end and (s.start_at - cur_end) < timedelta(
                hours=CONTINUITY_GAP_HOURS)
            if touches_before or touches_after:
                chain.append(s)
                remaining.remove(s)
                cur_start = min(cur_start, s.start_at)
                cur_end = max(cur_end, s.end_at)
                changed = True
    cont = (end - start).total_seconds() / 3600
    cont += sum((s.end_at - s.start_at).total_seconds() / 3600 for s in chain)
    ev.continuous_hours = round(cont, 2)
    if cont > HARD_MAX_CONTINUOUS_HOURS:
        ev.hard.append(Violation(
            "CONTINUOUS_SERVICE_LIMIT",
            f"连续服务将达 {ev.continuous_hours:g} 小时，"
            f"超过硬性上限 {HARD_MAX_CONTINUOUS_HOURS:g} 小时",
        ))
    elif cont > SOFT_MAX_CONTINUOUS_HOURS:
        ev.approvals.append(Violation(
            "CONTINUOUS_SERVICE_LONG",
            f"连续服务将达 {ev.continuous_hours:g} 小时，"
            f"超过软上限 {SOFT_MAX_CONTINUOUS_HOURS:g} 小时",
        ))

    # 6) 夜间服务组数（按班次开始日期统计）
    if is_night_shift(start, end):
        day = start.date()
        ev.night_shifts_that_day = sum(
            1 for s in others
            if s.start_at.date() == day and is_night_shift(s.start_at, s.end_at)
        )
        if ev.night_shifts_that_day >= MAX_NIGHT_SHIFTS_PER_DAY:
            ev.approvals.append(Violation(
                "NIGHT_SHIFT_LIMIT",
                f"当天已承担 {ev.night_shifts_that_day} 组夜间服务"
                f"（上限 {MAX_NIGHT_SHIFTS_PER_DAY} 组）",
            ))

    # 7) 普通偏好：负荷均衡 / 熟悉度 / 资质余量
    week_start = (start - timedelta(days=start.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    week_end = week_start + timedelta(days=7)
    week_load = sum(1 for s in others if week_start <= s.start_at < week_end)
    ev.score -= week_load * 5
    ev.notes.append(f"本周已排 {week_load} 班")
    if any(s.family_id == family.id for s in others):
        ev.score += 15
        ev.notes.append("曾服务该家庭，熟悉照护环境")
    margins = [(q.valid_until - end).days for q in worker.qualifications
               if q.skill in required and q.valid_until >= end]
    if margins and min(margins) >= 30:
        ev.score += 5
        ev.notes.append(f"资质余量充足（≥{min(margins)} 天）")

    return ev
