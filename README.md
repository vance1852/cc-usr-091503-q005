# 月嫂服务负荷编排

面向月嫂服务经理的负荷编排模块：员工临时请假时，根据**资质有效期、专项技能、服务对象需求、休息间隔、房间隔离要求和已确认班次**生成可解释的替班候选，并把规则分为三档——**硬性不符合 / 需要主管批准 / 普通偏好**。

后端：FastAPI + SQLite（SQLAlchemy 2.0），测试：pytest。

## 运行

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --reload        # 默认库文件 ./yuesao.db
.venv/bin/python -m pytest -q                  # 运行测试（28 个用例）
```

角色通过请求头模拟：经理 `X-Role: manager`；月嫂 `X-Role: worker` + `X-Worker-Id: <id>`。

## 规则分档（app/constraints.py，参数可按机构制度调整）

| 规则 | 硬性不符合（排除） | 需主管批准 |
|---|---|---|
| 专项技能/资质 | 缺少技能、资质不覆盖班次、**资质在班次中途到期** | — |
| 时间冲突 | 同一时间段已有其他班次（含跨日） | — |
| 房间隔离 | 隔离家庭班次前后 4h 缓冲内安排其他家庭 | — |
| 休息间隔 | < 2h | < 8h |
| 连续服务（间隔 <4h 视为连续） | > 16h | > 12h |
| 夜间服务（20:00–次日08:00） | — | 当天已 ≥2 组 |

普通偏好只影响排序：负荷均衡、曾服务该家庭、资质余量。

## 关键行为

- **任何调班不产生同时段重复承担**：写事务 `BEGIN IMMEDIATE` 串行化，事务内先校验后写入，并发调班只有一方成功（409 返回冲突依据）。
- **已开始的服务禁止静默换人**：`reassign` 对 `in_progress` 班次返回 409；须走 `emergency-replacement` 生成待确认交接单，`handovers/{id}/confirm` 确认后原班次截断、接替人从交接时刻承担剩余时段。
- **资质后来过期只影响尚未开始的安排**：`qualifications/{id}/expire` 扫描未开始班次并标记 `needs_reassignment`，`GET /api/reassignments-needed` 列出待重排班次与受影响家庭；进行中班次不受影响。
- **视图分离**：经理看候选排除依据、连续服务时长、必要交接、受影响家庭（`GET /api/adjustments/{id}` 回放一次调整）；月嫂只能看自己的安排与照护摘要（`GET /api/my/schedule`）。

## API 一览（前缀 /api）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /workers、/workers/{id}/qualifications、/families、/shifts | 基础资料与排班 |
| POST | /shifts/{id}/transition/{confirm,start,complete,cancel} | 班次状态机 |
| POST | /leave | 员工请假 → 受影响班次 + 三档候选评估 |
| GET | /shifts/{id}/candidates | 单班次候选评估（进行中班次只评估剩余时段） |
| POST | /shifts/{id}/reassign | 调班（`approve=true` 表示主管批准） |
| POST | /shifts/{id}/emergency-replacement | 紧急接替 → 待确认交接单 |
| POST | /handovers/{id}/confirm、/reject | 交接确认/驳回 |
| POST | /qualifications/{id}/expire | 资质到期 → 列出需重排对象 |
| GET | /reassignments-needed | 待重排班次及受影响家庭 |
| GET | /adjustments/{id} | 一次调整的完整回放 |
| GET | /my/schedule | 月嫂自助端（经理可带 worker_id 代查） |

## 测试重点（tests/）

- `test_cross_day.py`：跨日班次的夜班识别、时间重叠、休息间隔、连续服务时长
- `test_concurrent_swap.py`：并发调班不产生重复承担；并发确认交接只成功一次
- `test_qualification_expiry.py`：资质在班次中途到期；过期只影响未开始安排
- `test_candidates.py`：请假场景的三档分类与批准流程
- `test_handover.py`：禁止静默换人、交接确认流程
- `test_views.py`：经理/月嫂视图权限、房间隔离硬性约束
