"""数据模型：月嫂、资质、家庭、班次、交接、调整审计。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .utils import utcnow


class Base(DeclarativeBase):
    pass


# 参与负荷计算的班次状态（占用服务人员时间）
SHIFT_ACTIVE = ("scheduled", "confirmed", "in_progress")
# 尚未开始的班次状态（允许直接调班）
SHIFT_NOT_STARTED = ("scheduled", "confirmed")


class Worker(Base):
    """月嫂。"""

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    employee_no: Mapped[str] = mapped_column(String(20), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    qualifications: Mapped[list["Qualification"]] = relationship(
        back_populates="worker", cascade="all, delete-orphan"
    )


class Qualification(Base):
    """专项资质，带有效期；有效期须覆盖整个班次时段。"""

    __tablename__ = "qualifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    skill: Mapped[str] = mapped_column(String(50))
    valid_from: Mapped[datetime] = mapped_column(DateTime)
    valid_until: Mapped[datetime] = mapped_column(DateTime)

    worker: Mapped[Worker] = relationship(back_populates="qualifications")


class Family(Base):
    """服务对象家庭：照护需求、房间与隔离要求。"""

    __tablename__ = "families"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
    room: Mapped[str] = mapped_column(String(50), default="")
    isolation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    required_skills: Mapped[list] = mapped_column(JSON, default=list)
    care_notes: Mapped[str] = mapped_column(String(500), default="")


class Shift(Base):
    """班次：一名月嫂在一个时间段内服务一个家庭，支持跨日。"""

    __tablename__ = "shifts"
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_shift_time"),
        Index("ix_shift_worker_time", "worker_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    family_id: Mapped[int] = mapped_column(ForeignKey("families.id"))
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("workers.id"), nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    # scheduled / confirmed / in_progress / completed / cancelled
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    night_service: Mapped[bool] = mapped_column(Boolean, default=False)
    needs_reassignment: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    family: Mapped[Family] = relationship()
    worker: Mapped[Worker | None] = relationship()


class Handover(Base):
    """交接单：已开始服务的紧急接替必须先生成待确认交接。"""

    __tablename__ = "handovers"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    from_worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    to_worker_id: Mapped[int] = mapped_column(ForeignKey("workers.id"))
    reason: Mapped[str] = mapped_column(String(200), default="")
    handover_at: Mapped[datetime] = mapped_column(DateTime)
    # pending / confirmed / rejected
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    shift: Mapped[Shift] = relationship()


class Adjustment(Base):
    """调整审计：记录一次调班/接替的完整上下文，供经理回放。"""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    shift_id: Mapped[int] = mapped_column(ForeignKey("shifts.id"))
    kind: Mapped[str] = mapped_column(String(30))  # reassign / emergency_handover
    from_worker_id: Mapped[int | None] = mapped_column(nullable=True)
    to_worker_id: Mapped[int | None] = mapped_column(nullable=True)
    actor: Mapped[str] = mapped_column(String(50), default="manager")
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    handover_id: Mapped[int | None] = mapped_column(nullable=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
