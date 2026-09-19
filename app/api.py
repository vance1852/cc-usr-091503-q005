"""FastAPI 接口：经理视图与月嫂视图分离。

角色通过请求头传递（演示用）：
  X-User-Role: manager | nanny
  X-User-Id:   <员工或主管 id>
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel

from .db import connect, init_db, write_tx
from .repository import Repo
from .scheduler import SchedulingError, Scheduler, _HandoffRequired
from .timeutil import parse_dt

DB_PATH = "scheduling.db"

app = FastAPI(title="月嫂服务负荷编排", version="1.0")


def get_conn() -> sqlite3.Connection:
    if not hasattr(app.state, "conn"):
        app.state.conn = connect(DB_PATH)
        init_db(app.state.conn)
    return app.state.conn


def repo() -> Repo:
    return Repo(get_conn())


def require_manager(x_user_role: Optional[str] = Header(default=None)) -> None:
    if x_user_role != "manager":
        raise HTTPException(status_code=403, detail="仅服务经理可访问该资源")


def current_nanny_id(
    x_user_role: Optional[str] = Header(default=None),
    x_user_id: Optional[int] = Header(default=None),
) -> int:
    if x_user_role != "nanny" or x_user_id is None:
        raise HTTPException(status_code=403, detail="月嫂请以 X-User-Role: nanny 与 X-User-Id 访问")
    return x_user_id


class ReassignRequest(BaseModel):
    assignment_id: int
    to_employee_id: int
    approver_id: Optional[int] = None  # 存在需批准事项时必填


class HandoffRequest(BaseModel):
    assignment_id: int
    to_employee_id: int


def _handle_scheduler_error(exc: Exception) -> HTTPException:
    if isinstance(exc, _HandoffRequired):
        return HTTPException(status_code=409, detail={
            "code": "handoff_required",
            "message": str(exc),
            "handoff_id": exc.handoff_id,
        })
    if isinstance(exc, SchedulingError):
        return HTTPException(status_code=409, detail={"code": "rule_violation", "message": str(exc)})
    raise exc


# ------------------------------------------------------------------
# 经理接口
# ------------------------------------------------------------------
@app.get("/api/assignments/{assignment_id}/adjustment",
         dependencies=[Depends(require_manager)],
         tags=["manager"])
def get_adjustment(assignment_id: int):
    """查看一次调班的全部候选：排除依据、连续服务时长、交接要求、受影响家庭。"""
    s = Scheduler(repo())
    try:
        return s.build_adjustment(assignment_id)
    except SchedulingError as e:
        raise _handle_scheduler_error(e)


@app.post("/api/reassignments", tags=["manager"],
          dependencies=[Depends(require_manager)])
def reassign(req: ReassignRequest):
    """应用调班。在 BEGIN IMMEDIATE 事务内重新评估，杜绝并发重叠。"""
    conn = get_conn()
    s = Scheduler(Repo(conn))
    try:
        with write_tx(conn):
            return s.apply_reassignment(
                req.assignment_id, req.to_employee_id,
                approver_id=req.approver_id)
    except (SchedulingError, sqlite3.IntegrityError) as e:
        if isinstance(e, sqlite3.IntegrityError):
            raise HTTPException(status_code=409, detail={
                "code": "concurrent_conflict",
                "message": f"并发调班冲突：{e}",
            })
        raise _handle_scheduler_error(e)


@app.post("/api/handoffs", tags=["manager"],
          dependencies=[Depends(require_manager)])
def create_handoff(req: HandoffRequest):
    """为紧急接替创建交接单（服务已开始时调班前必须存在且已确认）。"""
    s = Scheduler(repo())
    try:
        with write_tx(get_conn()):
            hid = s.create_handoff(req.assignment_id, req.to_employee_id)
    except SchedulingError as e:
        raise _handle_scheduler_error(e)
    return {"handoff_id": hid, "acknowledged": False}


@app.post("/api/handoffs/{handoff_id}/acknowledge", tags=["handoff"])
def acknowledge_handoff(handoff_id: int):
    """交接确认（接替月嫂确认，或经理代确认）。"""
    s = Scheduler(repo())
    try:
        with write_tx(get_conn()):
            s.acknowledge_handoff(handoff_id)
    except SchedulingError as e:
        raise _handle_scheduler_error(e)
    return {"handoff_id": handoff_id, "acknowledged": True}


@app.get("/api/manager/expiring-certifications", tags=["manager"],
         dependencies=[Depends(require_manager)])
def expiring_certifications():
    """资质后来过期：只列出尚未开始、需要重排的安排。"""
    return {"items": Scheduler(repo()).scan_expired_certifications()}


# ------------------------------------------------------------------
# 月嫂接口：只可查看自己的安排及照护摘要
# ------------------------------------------------------------------
@app.get("/api/me/schedule", tags=["nanny"])
def my_schedule(
    since: datetime = Query(...),
    until: datetime = Query(...),
    nanny_id: int = Depends(current_nanny_id),
):
    if until <= since:
        raise HTTPException(status_code=422, detail="until 必须晚于 since")
    r = repo()
    rows = r.employee_assignments(nanny_id, parse_dt(since), parse_dt(until))
    return {
        "employee_id": nanny_id,
        "assignments": [
            {
                "id": row["id"],
                "family_id": row["family_id"],
                "family_name": row["family_name"],
                "starts_at": parse_dt(row["starts_at"]),
                "ends_at": parse_dt(row["ends_at"]),
                "care_summary": row["care_summary"],  # 照护摘要，不含经理备注
            }
            for row in rows
        ],
    }
