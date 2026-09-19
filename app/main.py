"""FastAPI 接口层：经理管理端 + 月嫂自助端。

角色通过请求头模拟：
- 经理：X-Role: manager
- 月嫂：X-Role: worker 且 X-Worker-Id: <id>（只能查看自己的安排）
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import services
from .database import make_engine, make_session_factory
from .models import Base, Family, Qualification, Shift, Worker
from .schemas import (
    EmergencyReplacementRequest,
    FamilyCreate,
    LeaveRequest,
    QualificationCreate,
    QualificationExpire,
    ReassignRequest,
    ShiftCreate,
    WorkerCreate,
)
from .services import ServiceError
from .utils import to_store


@dataclass
class Actor:
    role: str
    worker_id: int | None

    @property
    def name(self) -> str:
        return f"{self.role}:{self.worker_id}" if self.worker_id else self.role


def get_actor(
    x_role: str = Header(default="manager"),
    x_worker_id: int | None = Header(default=None),
) -> Actor:
    return Actor(role=x_role, worker_id=x_worker_id)


def require_manager(actor: Actor = Depends(get_actor)) -> Actor:
    if actor.role != "manager":
        raise HTTPException(status_code=403, detail="仅服务经理可执行该操作")
    return actor


def get_db(request: Request):
    session = request.app.state.session_factory()
    try:
        yield session
    finally:
        session.close()


def create_app(db_url: str = "sqlite:///./yuesao.db") -> FastAPI:
    app = FastAPI(title="月嫂服务负荷编排", version="1.0.0")
    engine = make_engine(db_url)
    Base.metadata.create_all(engine)
    app.state.engine = engine
    app.state.session_factory = make_session_factory(engine)

    @app.exception_handler(ServiceError)
    async def service_error_handler(_request: Request, exc: ServiceError):
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.message, "code": exc.code, **exc.payload},
        )

    # ================= 基础资料（经理） =================

    @app.post("/api/workers", status_code=201)
    def create_worker(data: WorkerCreate, db: Session = Depends(get_db),
                      actor: Actor = Depends(require_manager)):
        w = Worker(name=data.name, employee_no=data.employee_no)
        db.add(w)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            raise ServiceError(f"工号 {data.employee_no} 已存在")
        return services.worker_dict(w)

    @app.get("/api/workers")
    def list_workers(db: Session = Depends(get_db),
                     actor: Actor = Depends(require_manager)):
        return [services.worker_dict(w) for w in db.query(Worker).all()]

    @app.post("/api/workers/{worker_id}/qualifications", status_code=201)
    def add_qualification(worker_id: int, data: QualificationCreate,
                          db: Session = Depends(get_db),
                          actor: Actor = Depends(require_manager)):
        w = services.get_worker_or_404(db, worker_id)
        valid_from, valid_until = to_store(data.valid_from), to_store(data.valid_until)
        if valid_until <= valid_from:
            raise ServiceError("资质有效期止必须晚于有效期起")
        q = Qualification(worker_id=w.id, skill=data.skill,
                          valid_from=valid_from, valid_until=valid_until)
        db.add(q)
        db.commit()
        return services.qualification_dict(q)

    @app.post("/api/qualifications/{qual_id}/expire")
    def expire_qualification(qual_id: int, data: QualificationExpire,
                             db: Session = Depends(get_db),
                             actor: Actor = Depends(require_manager)):
        """资质到期（可指定时刻）。只影响尚未开始的安排，返回需要重排的对象。"""
        q, affected = services.expire_qualification(
            db, qual_id, data.effective_at, actor.name)
        return {
            "qualification": services.qualification_dict(q),
            "affected_count": len(affected),
            "affected_shifts": affected,
        }

    @app.post("/api/families", status_code=201)
    def create_family(data: FamilyCreate, db: Session = Depends(get_db),
                      actor: Actor = Depends(require_manager)):
        f = Family(name=data.name, room=data.room,
                   isolation_required=data.isolation_required,
                   required_skills=list(data.required_skills),
                   care_notes=data.care_notes)
        db.add(f)
        db.commit()
        return services.family_dict(f)

    @app.get("/api/families")
    def list_families(db: Session = Depends(get_db),
                      actor: Actor = Depends(require_manager)):
        return [services.family_dict(f) for f in db.query(Family).all()]

    # ================= 班次管理（经理） =================

    @app.post("/api/shifts", status_code=201)
    def create_shift(data: ShiftCreate, db: Session = Depends(get_db),
                     actor: Actor = Depends(require_manager)):
        shift, ev = services.create_shift(db, data, actor.name)
        return {**services.shift_dict(shift),
                "evaluation": ev.to_dict() if ev else None}

    @app.get("/api/shifts/{shift_id}")
    def get_shift(shift_id: int, db: Session = Depends(get_db),
                  actor: Actor = Depends(require_manager)):
        return services.shift_dict(services.get_shift_or_404(db, shift_id))

    @app.post("/api/shifts/{shift_id}/transition/{action}")
    def transition_shift(shift_id: int, action: str, db: Session = Depends(get_db),
                         actor: Actor = Depends(require_manager)):
        if action not in services._TRANSITIONS:
            raise ServiceError(f"未知操作 {action}")
        shift = services.transition_shift(db, shift_id, action, actor.name)
        return services.shift_dict(shift)

    # ================= 编排核心（经理） =================

    @app.get("/api/shifts/{shift_id}/candidates")
    def shift_candidates(shift_id: int, db: Session = Depends(get_db),
                         actor: Actor = Depends(require_manager)):
        """候选评估：硬性不符合 / 需主管批准 / 普通偏好 三档分组。"""
        return services.candidates_for_shift(db, shift_id)

    @app.post("/api/leave")
    def report_leave(data: LeaveRequest, db: Session = Depends(get_db),
                     actor: Actor = Depends(require_manager)):
        """员工请假：返回受影响班次、每个班次的候选评估、进行中班次提示。"""
        return services.report_leave(db, data, actor.name)

    @app.post("/api/shifts/{shift_id}/reassign")
    def reassign_shift(shift_id: int, data: ReassignRequest,
                       db: Session = Depends(get_db),
                       actor: Actor = Depends(require_manager)):
        """调班（仅限未开始班次）。已开始的服务禁止静默换人。"""
        shift, adj, ev = services.reassign_shift(db, shift_id, data, actor.name)
        return {
            "shift": services.shift_dict(shift),
            "adjustment_id": adj.id,
            "evaluation": ev.to_dict(),
        }

    @app.post("/api/shifts/{shift_id}/emergency-replacement", status_code=201)
    def emergency_replacement(shift_id: int, data: EmergencyReplacementRequest,
                              db: Session = Depends(get_db),
                              actor: Actor = Depends(require_manager)):
        """紧急接替：生成待确认交接单，确认前不换人。"""
        h, ev = services.emergency_replace(db, shift_id, data, actor.name)
        return {"handover": services.handover_dict(h),
                "evaluation": ev.to_dict()}

    @app.post("/api/handovers/{handover_id}/confirm")
    def confirm_handover(handover_id: int, db: Session = Depends(get_db),
                         actor: Actor = Depends(require_manager)):
        """交接确认：原班次截断，接替人从交接时刻起承担服务。"""
        h, new_shift, adj = services.confirm_handover(db, handover_id, actor.name)
        return {
            "handover": services.handover_dict(h),
            "new_shift": services.shift_dict(new_shift),
            "adjustment_id": adj.id,
        }

    @app.post("/api/handovers/{handover_id}/reject")
    def reject_handover(handover_id: int, db: Session = Depends(get_db),
                        actor: Actor = Depends(require_manager)):
        return services.handover_dict(
            services.reject_handover(db, handover_id, actor.name))

    @app.get("/api/reassignments-needed")
    def reassignments_needed(db: Session = Depends(get_db),
                             actor: Actor = Depends(require_manager)):
        """资质失效等待重排的班次及受影响家庭。"""
        return {"items": services.reassignments_needed(db)}

    @app.get("/api/adjustments/{adjustment_id}")
    def adjustment_view(adjustment_id: int, db: Session = Depends(get_db),
                        actor: Actor = Depends(require_manager)):
        """一次调整的完整回放：排除依据、连续服务时长、必要交接、受影响家庭。"""
        return services.adjustment_view(db, adjustment_id)

    # ================= 月嫂自助端 =================

    @app.get("/api/my/schedule")
    def my_schedule(actor: Actor = Depends(get_actor),
                    worker_id: int | None = None,
                    db: Session = Depends(get_db)):
        """月嫂查看自己的安排及所需照护摘要；经理可指定 worker_id 代查。"""
        if actor.role == "worker":
            if actor.worker_id is None:
                raise HTTPException(status_code=401, detail="缺少 X-Worker-Id 身份头")
            wid = actor.worker_id  # 月嫂只能看自己，忽略查询参数
        elif actor.role == "manager":
            if worker_id is None:
                raise ServiceError("经理代查需指定 worker_id 参数")
            wid = worker_id
        else:
            raise HTTPException(status_code=403, detail="未知角色")

        worker = services.get_worker_or_404(db, wid)
        shifts = (
            db.query(Shift)
            .filter(Shift.worker_id == wid, Shift.status.in_(services.SHIFT_ACTIVE))
            .order_by(Shift.start_at)
            .all()
        )
        return {
            "worker": services.worker_dict(worker),
            "shifts": [
                {
                    **services.shift_dict(s),
                    "care_summary": {
                        "family_name": s.family.name,
                        "room": s.family.room,
                        "isolation_required": s.family.isolation_required,
                        "required_skills": list(s.family.required_skills or []),
                        "care_notes": s.family.care_notes,
                    },
                }
                for s in shifts
            ],
        }

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
