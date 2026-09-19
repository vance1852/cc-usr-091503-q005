"""领域模型（Pydantic 出入参 + 领域常量）。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

from .timeutil import iso

# 规则分级
HARD = "hard"       # 硬性不符合：禁止安排
APPROVAL = "approval"  # 需要主管批准
PREFERENCE = "preference"  # 普通偏好

Severity = Literal["hard", "approval", "preference"]


class Issue(BaseModel):
    """一条可解释的规则判定依据。"""
    code: str
    severity: Severity
    message: str


class AssignmentView(BaseModel):
    id: int
    employee_id: int
    employee_name: str
    family_id: int
    family_name: str
    room_id: Optional[int] = None
    starts_at: datetime
    ends_at: datetime
    status: str
    supersedes_id: Optional[int] = None
    care_summary: str = ""

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.starts_at < end and start < self.ends_at


class CandidateView(BaseModel):
    employee_id: int
    employee_name: str
    eligible: bool                  # 无硬性问题
    needs_approval: bool            # 存在需主管批准的问题
    issues: list[Issue]
    consecutive_hours: float       # 含本班的连续服务时长
    prior_consecutive_hours: float  # 本班开始前的连续服务时长
    required_handoff: bool         # 是否需要紧急接替交接
    affected_family_ids: list[int]
    score: int = 0                 # 偏好评分，越高越优


class AdjustmentView(BaseModel):
    """经理查看一次调整时看到的完整解释。"""
    target_assignment_id: int
    family_id: int
    family_name: str
    care_summary: str
    starts_at: datetime
    ends_at: datetime
    in_progress: bool
    required_handoff: bool
    handoff_id: Optional[int] = None
    handoff_acknowledged_at: Optional[datetime] = None
    candidates: list[CandidateView]
    chosen: Optional[CandidateView] = None
    applied: bool = False
    new_assignment_id: Optional[int] = None
    affected_family_ids: list[int] = Field(default_factory=list)


class HandoffView(BaseModel):
    id: int
    assignment_id: int
    from_employee_id: int
    to_employee_id: int
    acknowledged_at: Optional[datetime] = None


class ExpiryItem(BaseModel):
    """资质到期对未开始班次的影响。"""
    employee_id: int
    employee_name: str
    certification: str
    valid_until: datetime
    affected_assignments: list[AssignmentView]


def assignment_view_from_row(row, care_summary: str = "") -> AssignmentView:
    return AssignmentView(
        id=row["id"],
        employee_id=row["employee_id"],
        employee_name=row["employee_name"],
        family_id=row["family_id"],
        family_name=row["family_name"],
        room_id=row["room_id"],
        starts_at=row["starts_at"],
        ends_at=row["ends_at"],
        status=row["status"],
        supersedes_id=row["supersedes_id"],
        care_summary=care_summary,
    )


def to_jsonable(obj):
    """FastAPI 直接序列化 datetime，此处保留给非 HTTP 场景使用。"""
    if isinstance(obj, datetime):
        return iso(obj)
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    return obj
