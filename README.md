# 月嫂服务负荷编排

服务经理临时收到请假通知时，系统根据**资质有效期、专项技能、服务对象需求、休息间隔、房间隔离要求和已确认班次**，
对可替班人员生成**可解释的候选安排**，并支持紧急接替的交接确认。

## 规则三级分类

| 级别 | 含义 | 判定项（code） |
|---|---|---|
| `hard` 硬性不符合 | 禁止安排，调班直接拒绝 | `INACTIVE`、`OVERLAP`、`CERT_MISSING`、`CERT_NOT_EFFECTIVE`、`CERT_EXPIRED`、`CERT_MIDSHIFT`（资质在班次中途到期）、`REST_GAP`（低于最低休息 8h）、`CONSECUTIVE_CAP`（默认 26h）、`HOURS_24_CAP`（滚动 24h 默认 18h）、`ISOLATION_STRICT`（严格隔离换房缓冲 < 12h） |
| `approval` 需主管批准 | 可排但请求须带 `approver_id` | `REST_SHORT`（休息 8–11h）、`CONSECUTIVE_WARN`、`HOURS_24_WARN`、`NIGHT_LOAD`（近 48h ≥2 组夜间服务）、`ISOLATION_STANDARD`（标准隔离缓冲 < 2h）、`CERT_EXPIRING_7D` |
| `preference` 普通偏好 | 不阻断，仅排序展示 | `SKILL_MISSING`（非必需技能）、`CERT_EXPIRING_30D`、`FAMILIARITY`（未服务过该家庭） |

关键语义：

- **任何调班都不能使同一员工在同一时间段重复承担服务**——引擎评估 + SQLite 触发器 `trg_no_overlap_insert` 双保险。
- **已经开始的服务不允许静默换人**：进行中的班次调班必须先走 `POST /api/handoffs` →
  `POST /api/handoffs/{id}/acknowledge` 完成紧急接替交接确认，否则返回 409 `handoff_required`。
- **资质后来过期只影响尚未开始的安排**：`GET /api/manager/expiring-certifications`
  仅列出 `starts_at > now` 且资质不能完整覆盖班次的已确认班次；进行中的班次不受影响。
- 经理一次调整可看到：每个候选人的排除依据（中文 message）、连续服务时长、是否需要交接、受影响家庭列表。
- 月嫂接口只返回**本人安排 + 照护摘要**，不含经理备注与他人安排。

## 技术栈

FastAPI + SQLite（标准库 `sqlite3`，WAL 模式；写操作用 `BEGIN IMMEDIATE` 串行化并发调班）+ Pydantic v2。
时间统一存 UTC ISO8601 字符串，正确支持**跨日服务**（如 22:00–次日 08:00）。

## 运行

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py --db scheduling.db --seed --port 8000
# 打开 http://127.0.0.1:8000/docs
```

角色通过请求头传入（演示鉴权）：

- 经理：`X-User-Role: manager`
- 月嫂：`X-User-Role: nanny`、`X-User-Id: <employee_id>`

## 接口

| 方法/路径 | 角色 | 说明 |
|---|---|---|
| `GET /api/assignments/{id}/adjustment` | 经理 | 候选安排与全部排除依据、连续时长、交接要求、受影响家庭 |
| `POST /api/reassignments` | 经理 | 调班（body：`assignment_id`、`to_employee_id`、可选 `approver_id`） |
| `POST /api/handoffs` | 经理 | 创建紧急接替交接单 |
| `POST /api/handoffs/{id}/acknowledge` | 接替人/经理 | 交接确认 |
| `GET /api/manager/expiring-certifications` | 经理 | 资质过期影响的未开始班次（需重排对象） |
| `GET /api/me/schedule?since=&until=` | 月嫂 | 本人安排及照护摘要 |

## 测试

```bash
.venv/bin/python -m pytest tests/ -q
```

重点覆盖：

- **跨日服务**：22:00–次日 08:00 夜班的休息间隔、连续服务链、滚动 24h 峰值；
- **并发调班**：两个经理同时把同一空档员工排进重叠时段，恰一个成功（200/409），
  并有 DB 触发器兜底（`test_concurrent_reassignment_only_one_wins`、`test_db_trigger_blocks_overlapping_insert`）；
- **资质在班次中途到期**：`CERT_MIDSHIFT` 硬性拦截；扫描时进行中的班次不受影响，仅列出未开始班次。

## 目录结构

```
app/
  db.py          # 建表、重叠触发器、BEGIN IMMEDIATE 写事务
  timeutil.py    # UTC/ISO8601 时间
  repository.py  # 查询与写入
  scheduler.py   # 规则引擎：候选评估、调班、交接、到期扫描
  models.py      # Pydantic 视图模型
  api.py         # FastAPI 路由与角色控制
  seed.py        # 演示数据
tests/           # 32 个用例
```
