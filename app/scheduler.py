"""编排引擎：规则评估、调班应用、交接确认、资质到期扫描。

规则分级
--------
hard（硬性不符合，禁止安排）:
  INACTIVE            员工已停用
  OVERLAP             同一时间段重复承担服务
  CERT_MISSING        缺少服务对象要求的资质
  CERT_NOT_EFFECTIVE  资质尚未生效
  CERT_EXPIRED        资质在班次开始前已过期
  CERT_MIDSHIFT       资质在班次进行中到期
  REST_GAP            与相邻班次休息间隔不足最低标准（硬性）
  REST_SHORT          休息间隔低于偏好时长，需主管批准
  CONSECUTIVE_CAP     连续服务时长超过上限
  HOURS_24_CAP        任意滚动 24 小时内服务时长超过上限
  ISOLATION_STRICT    严格隔离房间与其他房间服务缓冲不足

approval（需主管批准）:
  CONSECUTIVE_WARN    连续服务达到预警线
  HOURS_24_WARN       滚动 24 小时负荷达到预警线
  NIGHT_LOAD          24 小时内已承担两组（含）以上夜间服务
  ISOLATION_STANDARD  标准隔离房间换房缓冲偏紧
  CERT_EXPIRING_7D    资质将在班次结束后 7 天内到期

preference（普通偏好）:
  SKILL_MISSING       缺少家庭期望的专项技能（如早产儿照护）
  CERT_EXPIRING_30D   资质将在班次结束后 30 天内到期
  FAMILIARITY         未曾服务过该家庭（连续性偏好）
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import (
    APPROVAL,
    HARD,
    PREFERENCE,
    AdjustmentView,
    AssignmentView,
    CandidateView,
    ExpiryItem,
    Issue,
    assignment_view_from_row,
)
from .repository import Repo
from .timeutil import iso, now_utc, parse_dt

# 隔离缓冲（小时）
STRICT_BUFFER_HOURS = 12.0
STANDARD_BUFFER_HOURS = 2.0
# 资质到期预警
CERT_APPROVAL_DAYS = 7
CERT_PREF_DAYS = 30
# 夜间时段
NIGHT_START_HOUR = 22
NIGHT_END_HOUR = 6


class SchedulingError(Exception):
    """业务校验失败，HTTP 层映射为 409。"""


@dataclass
class _Interval:
    start: datetime
    end: datetime
    row: sqlite3.Row

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


def _view(row: sqlite3.Row) -> AssignmentView:
    return assignment_view_from_row(row, row["care_summary"] if "care_summary" in row.keys() else "")


def _split(value: str | None) -> set[str]:
    if not value:
        return set()
    return {v.strip() for v in value.split(",") if v.strip()}


class Scheduler:
    def __init__(self, repo: Repo):
        self.repo = repo

    # ------------------------------------------------------------------
    # 候选评估
    # ------------------------------------------------------------------
    def build_adjustment(self, assignment_id: int, at: datetime | None = None) -> AdjustmentView:
        at = at or now_utc()
        target_row = self.repo.get_assignment(assignment_id)
        if target_row is None:
            raise SchedulingError(f"班次 {assignment_id} 不存在")
        target = _view(target_row)
        family = self.repo.get_family(target.family_id)
        room = self.repo.get_room(target.room_id)
        in_progress = at >= target.starts_at
        required_handoff = in_progress and at < target.ends_at

        candidates = [
            self._evaluate_employee(emp, target, family, room, assignment_id, at)
            for emp in self.repo.list_employees(active_only=False)
            if emp["id"] != target.employee_id
        ]
        candidates.sort(key=lambda c: (not c.eligible, c.needs_approval, -c.score, c.employee_id))

        affected = sorted({
            fid for c in candidates if c.eligible for fid in c.affected_family_ids
        })

        return AdjustmentView(
            target_assignment_id=assignment_id,
            family_id=target.family_id,
            family_name=target.family_name,
            care_summary=family["care_summary"],
            starts_at=target.starts_at,
            ends_at=target.ends_at,
            in_progress=required_handoff,
            required_handoff=required_handoff,
            candidates=candidates,
            affected_family_ids=affected,
        )

    def _evaluate_employee(
        self, emp: sqlite3.Row, target: AssignmentView, family: sqlite3.Row,
        room: sqlite3.Row | None, exclude_id: int, at: datetime
    ) -> CandidateView:
        issues: list[Issue] = []
        start, end = target.starts_at, target.ends_at

        rows = self.repo.employee_assignments(
            emp["id"], start - timedelta(hours=48), end + timedelta(hours=48),
            exclude_id=exclude_id,
        )
        intervals = [_Interval(parse_dt(r["starts_at"]), parse_dt(r["ends_at"]), r)
                     for r in rows]

        if not emp["active"]:
            issues.append(Issue(code="INACTIVE", severity=HARD,
                                message="该员工已停用，不可安排"))

        # 1) 时间段重叠（同一时间段重复承担服务）
        overlaps = [iv for iv in intervals if iv.start < end and start < iv.end]
        for iv in overlaps:
            issues.append(Issue(
                code="OVERLAP", severity=HARD,
                message=f"与已确认班次 #{iv.row['id']}（{iv.row['family_name']}，"
                        f"{iso(iv.start)}–{iso(iv.end)}）时间段重叠"))

        usable = [iv for iv in intervals if iv not in overlaps]
        prior = sorted((iv for iv in usable if iv.end <= start),
                       key=lambda iv: iv.end)
        later = sorted((iv for iv in usable if iv.start >= end),
                       key=lambda iv: iv.start)

        # 2) 休息间隔 + 连续服务链
        rest_min = float(emp["rest_min_hours"])    # 硬性下限（默认 8h）
        rest_pref = float(emp["rest_pref_hours"])  # 偏好间隔（默认 11h）
        ordered = sorted(usable, key=lambda iv: iv.start)
        prev = prior[-1] if prior else None
        if prev is not None:
            gap_h = (start - prev.end).total_seconds() / 3600
            if gap_h < rest_min:
                issues.append(Issue(
                    code="REST_GAP", severity=HARD,
                    message=f"距上一班次 #{prev.row['id']}（{prev.row['family_name']}）"
                            f"仅间隔 {gap_h:.1f} 小时，低于最低休息 {rest_min:g} 小时"))
            elif gap_h < rest_pref:
                issues.append(Issue(
                    code="REST_SHORT", severity=APPROVAL,
                    message=f"距上一班次 #{prev.row['id']}（{prev.row['family_name']}）"
                            f"仅间隔 {gap_h:.1f} 小时，低于偏好休息 {rest_pref:g} 小时，需主管批准"))
        nxt = later[0] if later else None
        if nxt is not None:
            gap_h = (nxt.start - end).total_seconds() / 3600
            if gap_h < rest_min:
                issues.append(Issue(
                    code="REST_GAP", severity=HARD,
                    message=f"距下一班次 #{nxt.row['id']}（{nxt.row['family_name']}）"
                            f"仅间隔 {gap_h:.1f} 小时，低于最低休息 {rest_min:g} 小时"))
            elif gap_h < rest_pref:
                issues.append(Issue(
                    code="REST_SHORT", severity=APPROVAL,
                    message=f"距下一班次 #{nxt.row['id']}（{nxt.row['family_name']}）"
                            f"仅间隔 {gap_h:.1f} 小时，低于偏好休息 {rest_pref:g} 小时，需主管批准"))

        # 连续服务链：沿间隔低于偏好休息的班次向前追溯起点（间隔本身计入连续时长）
        chain_start = start
        if prev is not None and (start - prev.end).total_seconds() / 3600 < rest_pref:
            chain_start = prev.start
            idx = ordered.index(prev)
            while idx > 0:
                g = (ordered[idx].start - ordered[idx - 1].end).total_seconds() / 3600
                if g >= rest_pref:
                    break
                chain_start = ordered[idx - 1].start
                idx -= 1

        prior_consec_h = (start - chain_start).total_seconds() / 3600 if prev else 0.0
        consec_h = (end - chain_start).total_seconds() / 3600
        cap = float(emp["max_consecutive_hours"])
        warn = float(emp["consecutive_warn_hours"])
        if consec_h > cap:
            issues.append(Issue(
                code="CONSECUTIVE_CAP", severity=HARD,
                message=f"连续服务时长 {consec_h:.1f} 小时，超过上限 {cap:g} 小时"))
        elif consec_h >= warn:
            issues.append(Issue(
                code="CONSECUTIVE_WARN", severity=APPROVAL,
                message=f"连续服务时长 {consec_h:.1f} 小时，达到预警线 {warn:g} 小时，需主管批准"))

        # 3) 滚动 24 小时负荷
        peak_hours = self._peak_24h_hours(usable, start, end)
        cap24 = float(emp["max_hours_per_24h"])
        if peak_hours > cap24:
            issues.append(Issue(
                code="HOURS_24_CAP", severity=HARD,
                message=f"滚动 24 小时内服务 {peak_hours:.1f} 小时，超过上限 {cap24:g} 小时"))
        elif peak_hours >= cap24 - 2:
            issues.append(Issue(
                code="HOURS_24_WARN", severity=APPROVAL,
                message=f"滚动 24 小时内服务 {peak_hours:.1f} 小时，接近上限 {cap24:g} 小时，需主管批准"))

        # 4) 近 48 小时夜间服务组数
        night_count = self._night_services(usable, start)
        if night_count >= 2:
            issues.append(Issue(
                code="NIGHT_LOAD", severity=APPROVAL,
                message=f"近 48 小时已承担 {night_count} 组夜间服务，连续作战需主管批准"))

        # 5) 资质（有效期必须完整覆盖班次）
        self._check_certifications(emp, family, start, end, issues)

        # 6) 专项技能（偏好）
        have_skills = self.repo.skills(emp["id"])
        preferred = _split(family["preferred_skills"])
        matched = preferred & have_skills
        for missing in sorted(preferred - have_skills):
            issues.append(Issue(
                code="SKILL_MISSING", severity=PREFERENCE,
                message=f"缺少家庭期望的专项技能：{missing}"))

        # 7) 房间隔离
        if room is not None:
            self._check_isolation(emp, room, usable, start, end, issues)

        # 8) 连续性偏好
        score = len(matched) * 10
        if self.repo.served_family_before(emp["id"], family["id"], start, exclude_id):
            score += 5
        else:
            issues.append(Issue(
                code="FAMILIARITY", severity=PREFERENCE,
                message="此前未服务过该家庭，缺乏照护连续性"))
        score -= sum(1 for i in issues if i.severity == PREFERENCE) * 2

        affected = sorted({
            iv.row["family_id"] for iv in intervals
            if start - timedelta(hours=24) < iv.end and iv.start < end + timedelta(hours=24)
        })

        return CandidateView(
            employee_id=emp["id"],
            employee_name=emp["name"],
            eligible=not any(i.severity == HARD for i in issues),
            needs_approval=any(i.severity == APPROVAL for i in issues),
            issues=issues,
            consecutive_hours=round(consec_h, 2),
            prior_consecutive_hours=round(prior_consec_h, 2),
            required_handoff=at >= start and at < end,
            affected_family_ids=affected,
            score=score,
        )

    @staticmethod
    def _peak_24h_hours(intervals: list[_Interval], start: datetime, end: datetime) -> float:
        """含目标班次在内，任意滚动 24 小时窗口内的最大服务小时数。

        负荷在窗口滑动时的极值出现在事件边界：班次进入窗口（ws=iv.start）
        与班次离开窗口（ws=iv.end-24h）。
        """
        target = _Interval(start, end, None)  # type: ignore[arg-type]
        all_iv = intervals + [target]
        window_starts = {start, end - timedelta(hours=24)}
        for iv in intervals:
            if start - timedelta(hours=24) <= iv.start <= end:
                window_starts.add(iv.start)
                window_starts.add(iv.end - timedelta(hours=24))
        peak = 0.0
        for ws in window_starts:
            we = ws + timedelta(hours=24)
            secs = 0.0
            for iv in all_iv:
                s = max(iv.start, ws)
                e = min(iv.end, we)
                if e > s:
                    secs += (e - s).total_seconds()
            peak = max(peak, secs / 3600)
        return peak

    @staticmethod
    def _night_services(intervals: list[_Interval], start: datetime) -> int:
        """统计近 48 小时内触及夜间时段（22:00–06:00）的已承担班次数。"""
        lo, hi = start - timedelta(hours=48), start
        count = 0
        for iv in intervals:
            iv_end = min(iv.end, hi)
            iv_start = max(iv.start, lo)
            if iv_end <= iv_start:
                continue
            t = iv_start
            while t < iv_end:
                h = t.hour
                if h >= NIGHT_START_HOUR or h < NIGHT_END_HOUR:
                    count += 1
                    break
                t += timedelta(hours=1)
        return count

    def _check_certifications(
        self, emp: sqlite3.Row, family: sqlite3.Row,
        start: datetime, end: datetime, issues: list[Issue]
    ) -> None:
        required = _split(family["required_certs"])
        if not required:
            return
        certs = self.repo.certifications(emp["id"])
        by_name: dict[str, list[sqlite3.Row]] = {}
        for c in certs:
            by_name.setdefault(c["name"], []).append(c)

        for name in sorted(required):
            held = by_name.get(name)
            if not held:
                issues.append(Issue(
                    code="CERT_MISSING", severity=HARD,
                    message=f"缺少必需资质：{name}"))
                continue
            latest = max(held, key=lambda c: parse_dt(c["valid_until"]))
            vf, vu = parse_dt(latest["valid_from"]), parse_dt(latest["valid_until"])
            if vf > start:
                issues.append(Issue(
                    code="CERT_NOT_EFFECTIVE", severity=HARD,
                    message=f"资质「{name}」{iso(vf)} 才生效，晚于班次开始"))
            elif vu <= start:
                issues.append(Issue(
                    code="CERT_EXPIRED", severity=HARD,
                    message=f"资质「{name}」已于 {iso(vu)} 过期"))
            elif vu < end:
                issues.append(Issue(
                    code="CERT_MIDSHIFT", severity=HARD,
                    message=f"资质「{name}」将于 {iso(vu)} 在班次进行中到期，"
                            f"不能覆盖整个班次"))
            elif vu <= end + timedelta(days=CERT_APPROVAL_DAYS):
                issues.append(Issue(
                    code="CERT_EXPIRING_7D", severity=APPROVAL,
                    message=f"资质「{name}」将于 {iso(vu)} 到期（班次结束后 7 天内），需主管批准"))
            elif vu <= end + timedelta(days=CERT_PREF_DAYS):
                issues.append(Issue(
                    code="CERT_EXPIRING_30D", severity=PREFERENCE,
                    message=f"资质「{name}」将于 {iso(vu)} 到期（30 天内），请关注续期"))

    @staticmethod
    def _check_isolation(
        emp: sqlite3.Row, room: sqlite3.Row, intervals: list[_Interval],
        start: datetime, end: datetime, issues: list[Issue]
    ) -> None:
        level = room["isolation"]
        if level == "none":
            return
        buffer_h = STRICT_BUFFER_HOURS if level == "strict" else STANDARD_BUFFER_HOURS
        nearest: tuple[float, _Interval] | None = None
        for iv in intervals:
            if iv.row["room_id"] == room["id"]:
                continue  # 同一房间不涉及隔离
            if iv.start < end and start < iv.end:
                continue  # 重叠已由 OVERLAP 记录
            if iv.end <= start:
                gap = (start - iv.end).total_seconds() / 3600
            else:
                gap = (iv.start - end).total_seconds() / 3600
            if gap < buffer_h and (nearest is None or gap < nearest[0]):
                nearest = (gap, iv)
        if nearest is None:
            return
        gap, iv = nearest
        if level == "strict":
            issues.append(Issue(
                code="ISOLATION_STRICT", severity=HARD,
                message=f"目标房间要求严格隔离，但 {gap:.1f} 小时后需在另一房间"
                        f"（{iv.row['family_name']}）服务，缓冲不足 {buffer_h:g} 小时"))
        else:
            issues.append(Issue(
                code="ISOLATION_STANDARD", severity=APPROVAL,
                message=f"目标房间标准隔离，与另一房间（{iv.row['family_name']}）"
                        f"仅间隔 {gap:.1f} 小时（建议 {buffer_h:g} 小时），需主管批准"))

    # ------------------------------------------------------------------
    # 调班应用
    # ------------------------------------------------------------------
    def apply_reassignment(
        self, assignment_id: int, to_employee_id: int,
        approver_id: int | None = None, handoff_id: int | None = None,
        at: datetime | None = None
    ) -> AdjustmentView:
        """在已持有的写事务中应用调班。

        - 硬性不符合的候选人拒绝；
        - 需主管批准的问题必须提供 approver_id；
        - 已经开始的服务必须先完成紧急接替交接确认，且不允许静默换人。
        """
        at = at or now_utc()
        adj = self.build_adjustment(assignment_id, at=at)
        if at >= adj.ends_at:
            raise SchedulingError("班次已结束，不能调班")
        candidate = next((c for c in adj.candidates
                          if c.employee_id == to_employee_id), None)
        if candidate is None:
            raise SchedulingError("候选人为当前值班人或不存在")
        hard = [i for i in candidate.issues if i.severity == HARD]
        if hard:
            raise SchedulingError(
                "候选人存在硬性不符合项，不可调班：" + "；".join(i.message for i in hard))

        target_row = self.repo.get_assignment(assignment_id)
        if target_row["status"] != "confirmed":
            raise SchedulingError(f"班次 {assignment_id} 已失效，不能再次调班")

        if candidate.needs_approval and approver_id is None:
            raise SchedulingError(
                "该候选人存在需主管批准的事项，必须提供 approver_id")

        acknowledged = None
        if adj.in_progress:
            # 已开始服务：不允许静默换人，必须先创建并完成紧急接替交接确认
            # （POST /api/handoffs -> /api/handoffs/{id}/acknowledge）。
            acknowledged = self.repo.find_acknowledged_handoff(
                assignment_id, to_employee_id)
            if acknowledged is None:
                raise _HandoffRequired(
                    -1, "服务已经开始，紧急接替须先创建交接单并经接替人确认，"
                        "不允许静默换人")
        elif handoff_id is not None:
            h = self.repo.get_handoff(handoff_id)
            if h is None or h["to_employee_id"] != to_employee_id:
                raise SchedulingError("交接记录与候选人不匹配")
            if h["acknowledged_at"] is None:
                raise _HandoffRequired(
                    handoff_id, "交接尚未确认，不能调班")

        new_id = self.repo.supersede_and_create(
            assignment_id, to_employee_id, adj.family_id,
            target_row["room_id"], adj.starts_at, adj.ends_at, at)
        adj.chosen = candidate
        adj.applied = True
        adj.new_assignment_id = new_id
        adj.handoff_id = acknowledged["id"] if acknowledged else None
        if acknowledged is not None:
            adj.handoff_acknowledged_at = parse_dt(acknowledged["acknowledged_at"])
        return adj

    def create_handoff(self, assignment_id: int, to_employee_id: int,
                       at: datetime | None = None) -> int:
        at = at or now_utc()
        target_row = self.repo.get_assignment(assignment_id)
        if target_row is None:
            raise SchedulingError(f"班次 {assignment_id} 不存在")
        if to_employee_id == target_row["employee_id"]:
            raise SchedulingError("接替人不能是当前值班人")
        return self.repo.create_handoff(
            assignment_id, target_row["employee_id"], to_employee_id, at)

    def acknowledge_handoff(self, handoff_id: int, at: datetime | None = None) -> None:
        at = at or now_utc()
        h = self.repo.get_handoff(handoff_id)
        if h is None:
            raise SchedulingError("交接记录不存在")
        if h["acknowledged_at"] is not None:
            raise SchedulingError("该交接已确认，请勿重复操作")
        self.repo.acknowledge_handoff(handoff_id, at)

    # ------------------------------------------------------------------
    # 资质到期扫描：只影响尚未开始的安排
    # ------------------------------------------------------------------
    def scan_expired_certifications(self, at: datetime | None = None) -> list[ExpiryItem]:
        """找出资质现已过期（或在未开始班次中途过期）的已确认、未开始班次。"""
        at = at or now_utc()
        grouped: dict[tuple[int, str], ExpiryItem] = {}
        for row in self.repo.future_assignments(at):
            start, end = parse_dt(row["starts_at"]), parse_dt(row["ends_at"])
            if start <= at:
                continue  # 已经开始/进行中的安排不受影响
            family = self.repo.get_family(row["family_id"])
            required = _split(family["required_certs"])
            for c in self.repo.certifications(row["employee_id"]):
                if c["name"] not in required:
                    continue
                vu = parse_dt(c["valid_until"])
                if vu < end:  # 已过期或将在该班次结束前过期（含班次中途到期）
                    key = (row["employee_id"], c["name"])
                    item = grouped.get(key)
                    if item is None:
                        item = ExpiryItem(
                            employee_id=row["employee_id"],
                            employee_name=row["employee_name"],
                            certification=c["name"],
                            valid_until=vu,
                            affected_assignments=[],
                        )
                        grouped[key] = item
                    item.affected_assignments.append(_view(row))
        return [grouped[k] for k in sorted(grouped)]


class _HandoffRequired(SchedulingError):
    """紧急接替缺少交接确认。携带待确认的 handoff_id。"""

    def __init__(self, handoff_id: int, message: str):
        super().__init__(message)
        self.handoff_id = handoff_id
