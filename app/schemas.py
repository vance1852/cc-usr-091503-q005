"""接口请求模型。"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class WorkerCreate(BaseModel):
    name: str
    employee_no: str


class QualificationCreate(BaseModel):
    skill: str
    valid_from: datetime
    valid_until: datetime


class QualificationExpire(BaseModel):
    # 不传表示立即到期；可指定时刻以模拟"班次中途到期"
    effective_at: datetime | None = None


class FamilyCreate(BaseModel):
    name: str
    room: str = ""
    isolation_required: bool = False
    required_skills: list[str] = Field(default_factory=list)
    care_notes: str = ""


class ShiftCreate(BaseModel):
    family_id: int
    worker_id: int | None = None
    start_at: datetime
    end_at: datetime
    # 创建时若触发"需主管批准"规则，主管可一并批准
    approve: bool = False


class ReassignRequest(BaseModel):
    worker_id: int
    approve: bool = False
    reason: str = ""


class EmergencyReplacementRequest(BaseModel):
    worker_id: int
    reason: str = ""
    approve: bool = False


class LeaveRequest(BaseModel):
    worker_id: int
    start_at: datetime
    end_at: datetime
    reason: str = ""
