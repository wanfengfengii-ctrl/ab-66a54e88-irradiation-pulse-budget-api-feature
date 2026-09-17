# 材料辐照脉冲预算 API

FastAPI + SQLAlchemy + SQLite 实现的纯后端服务，用于管理辐照样品批次的脉冲预算：

- 批次创建时核定预算，**不可追加**（无追加预算接口）；
- 每次授权在**单个事务**内完成「余额检查 → 扣减 → 账本登记」；
- `request_key` 为**全局幂等键**：控制软件超时重试、两台终端同时申请，都不会重复扣减；
- 全部状态持久化在 SQLite，**进程重启后数据与重放结果不丢失**。

## 快速开始

### Docker（推荐）

```bash
# 启动 API（宿主端口默认 8000，可用 API_PORT 覆盖）
docker compose up --build api
API_PORT=9000 docker compose up --build api

# 启动 API 并运行 verify 一次性验收服务（验收结束后整体退出，
# 并以 verify 的退出码作为命令退出码，便于 CI 门禁）
docker compose up --build --abort-on-container-exit --exit-code-from verify

# 或者在 API 已运行时单独执行验收
docker compose run --rm verify
```

数据保存在命名卷 `api-data`（容器内 `/data/app.db`），容器重建、进程重启均不丢数据。

`api` 与 `verify` 共用同一个镜像（仅由 `api` 服务的 `build` 段构建一次，`verify` 直接复用，避免并行构建争用同一镜像名）；若尚未构建过镜像，`docker compose run --rm verify` 前请先执行 `docker compose build`。

### 本地运行（Python 3.12）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
# 自定义数据库位置：
DATABASE_URL=sqlite:////tmp/irradiation.db uvicorn app.main:app
```

### 测试与验收

```bash
pytest                 # 单元/集成测试：并发扣减、重试重放、进程重启持久化
python verify.py       # 对运行中的 API 执行一次性验收（默认 http://localhost:${API_PORT:-8000}）
```

## 接口

### `POST /batches` — 创建批次

请求体：

```json
{"batch_id": "lot-2026-001", "budget": 100}
```

- `batch_id`：非空字符串，全局唯一；
- `budget`：正整数（≤ 2^63-1）。

响应：

- **201** `{"batch_id": "lot-2026-001", "budget": 100, "remaining": 100}` — 成功时固定返回 201；
- **409** `BATCH_ALREADY_EXISTS` — `batch_id` 已存在（含并发创建同一 id）；
- **422** `VALIDATION_ERROR` — 字段缺失、类型错误或预算非正整数。

### `POST /authorizations` — 申请脉冲授权（幂等扣减）

请求体：

```json
{"batch_id": "lot-2026-001", "request_key": "req-8f3a", "pulses": 10}
```

- `request_key`：非空字符串，**全局唯一**（跨批次唯一），由调用方按请求生成；
- `pulses`：正整数。

成功响应（**201**，首次成功与重放均固定返回 201）：

```json
{
  "authorization_id": 1,
  "request_key": "req-8f3a",
  "batch_id": "lot-2026-001",
  "pulses": 10,
  "remaining": 90
}
```

`remaining` 是**本次授权扣减后的余额快照**；重放时返回首次成功时记录的快照，而不是当前余额。

错误响应：

- **404** `BATCH_NOT_FOUND` — 批次不存在；
- **409** `INSUFFICIENT_BUDGET` — 余额不足，**不创建授权、不写账本**；
- **409** `REQUEST_KEY_CONFLICT` — 同一 `request_key` 携带了不同的 `batch_id` 或 `pulses`；
- **422** `VALIDATION_ERROR` — 请求体非法。

### 查询接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/batches/{batch_id}` | 查询批次预算与当前余额（404 `BATCH_NOT_FOUND`） |
| `GET` `/authorizations/{request_key}` | 查询账本中的授权记录（404 `AUTHORIZATION_NOT_FOUND`） |
| `GET` `/batches/{batch_id}/authorizations` | 批次授权明细分页核对（快照稳定，见下） |
| `GET` `/health` | 健康检查，返回 `{"status": "ok"}` |

### `GET /batches/{batch_id}/authorizations` — 批次授权明细核对（分页）

辐照实验结束后，值班人员按批次顺序核对每一次脉冲授权。首次请求只带 `page_size`（可省略，默认 20，范围 1–100）：

```
GET /batches/pb-1/authorizations?page_size=20
```

响应：

```json
{
  "batch_id": "pb-1",
  "items": [
    {"authorization_id": 1, "request_key": "req-1", "batch_id": "pb-1", "pulses": 10, "remaining": 90},
    {"authorization_id": 2, "request_key": "req-2", "batch_id": "pb-1", "pulses": 20, "remaining": 70}
  ],
  "used_pulses": 50,
  "snapshot_remaining": 50,
  "snapshot_budget": 100,
  "snapshot_max_id": 5,
  "next_position": 2
}
```

- `items`：按授权编号（`authorization_id`）升序排列的本页授权，`remaining` 是该次扣减后的余额快照；
- `used_pulses`：**整个快照视图**（而非仅本页）的累计已用脉冲；
- `snapshot_remaining` / `snapshot_budget`：核对时点的快照余额与预算上限（`snapshot_remaining = snapshot_budget - used_pulses`）；
- `snapshot_max_id`：本轮视图的固定上限（该批次首次查询时的最大授权编号），空批次为 `0`；
- `next_position`：下一页位置（本页最后一条的授权编号）；末页为 `null`。

后续请求必须同时携带首页返回的 `snapshot_max_id` 与上一页的 `next_position`：

```
GET /batches/pb-1/authorizations?page_size=20&snapshot_max_id=5&position=2
```

**快照稳定性**：服务层在首次查询时以该批次的最大授权编号固定本轮视图，之后所有页面与所有统计值都只计算 `authorization_id <= snapshot_max_id` 的授权。分页期间新产生的授权既不会混入后续页面，也不会把旧记录挤出窗口（无遗漏）；其扣减照常进行，但要等值班人员重新发起一轮核对（新的首次请求）才会出现。

错误响应：

- **404** `BATCH_NOT_FOUND` — 批次不存在（沿用现有机器码）；
- **400** `PAGINATION_ERROR` — 分页参数非法且互不矛盾，包括：
  - `page_size` 不是 1–100 的整数；
  - `snapshot_max_id` / `position` 不是整数（`position` 须为正整数，`snapshot_max_id` 须为非负整数）；
  - 只提供了二者之一（两者必须同时出现，或同时不出现）；
  - `position` 不属于该批次（含编号不存在、属于其他批次），或 `position >= snapshot_max_id`；
  - `snapshot_max_id` 不是该批次的既有授权编号。

该接口为只读：任何分页参数错误都不会影响授权扣减与余额。

## 重试与幂等语义

`request_key` 是请求的全局幂等键，服务端以账本表 `authorizations` 的唯一约束保证：

1. **首次请求**：仅当余额充足时，在单个事务中创建授权记录并扣减余额，返回 201、授权编号与扣减后余额；余额不足返回 409 `INSUFFICIENT_BUDGET`，且不产生任何授权或账本记录（该键未绑定，之后用同键发起的请求按新请求处理）。
2. **同键同字段重试**（`batch_id`、`pulses` 均相同）：重放首次成功的状态码（201）与响应体，**不会再次扣账**——无论首次成功发生在本次调用之前多久、是否经历了进程重启。
3. **同键不同字段**（`batch_id` 或 `pulses` 任一不同）：返回 409 `REQUEST_KEY_CONFLICT`，不扣账。
4. **并发同键**：多个并发请求携带同一 `request_key` 时，只有一个真正执行扣减，其余请求重放赢家的响应；若字段不一致，败者收到 409 `REQUEST_KEY_CONFLICT`。
5. **失败不落账**：余额不足、批次不存在等失败不写入账本，因此失败请求的重试会被当作新请求重新评估。

## 并发与一致性保证

- 扣减使用**单条原子条件 UPDATE**（`UPDATE batches SET remaining = remaining - :pulses WHERE batch_id = :batch_id AND remaining >= :pulses`），配合 SQLite 写事务串行化（`BEGIN IMMEDIATE` + 30s busy timeout + WAL），任意交错下：
  - 成功脉冲总量 ≤ 初始预算；
  - 余额永不为负（另有 `CHECK (remaining >= 0)` 约束兜底）；
  - 扣减与账本登记同事务提交或回滚，不会出现「扣了账没记账」或「记了账没扣款」。
- 并发成功响应中的 `remaining` 构成不重复的递减序列，可用于审计对账。
- 不提供追加预算接口；预算只在创建批次时核定。

## 错误响应

所有错误响应使用统一信封与稳定机器码：

```json
{"code": "INSUFFICIENT_BUDGET", "message": "batch 'lot-1' has 0 pulses remaining, requested 10"}
```

| 机器码 | HTTP 状态 | 含义 |
| --- | --- | --- |
| `BATCH_ALREADY_EXISTS` | 409 | 批次 ID 已存在 |
| `BATCH_NOT_FOUND` | 404 | 批次不存在 |
| `INSUFFICIENT_BUDGET` | 409 | 余额不足，未扣减、未落账 |
| `REQUEST_KEY_CONFLICT` | 409 | 同一 request_key 携带不同业务字段 |
| `AUTHORIZATION_NOT_FOUND` | 404 | 账本中无此 request_key |
| `PAGINATION_ERROR` | 400 | 批次授权核对分页参数非法或互相矛盾（不影响授权扣减） |
| `VALIDATION_ERROR` | 422 | 请求体校验失败（附 `details` 字段定位） |
| `NOT_FOUND` | 404 | 路由不存在 |
| `INTERNAL_ERROR` | 500 | 未预期的服务端错误 |

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `sqlite:///./data/app.db`（容器内为 `sqlite:////data/app.db`） | SQLite 连接串，数据落盘即持久化 |
| `API_PORT` | `8000` | docker compose 暴露的宿主端口 |
| `API_BASE_URL` | `http://localhost:${API_PORT:-8000}` | `verify.py` 的目标地址（compose 内为 `http://api:8000`） |

## 项目结构

```
app/
  main.py       # FastAPI 应用工厂与路由（uvicorn app.main:app）
  services.py   # 核心业务：单事务原子扣减 + 幂等重放
  models.py     # batches / authorizations（幂等账本）两张表
  database.py   # SQLite 引擎：WAL、busy_timeout、BEGIN IMMEDIATE
  schemas.py    # Pydantic 请求/响应模型
  errors.py     # 稳定机器码错误信封
tests/          # pytest：并发、重试、进程重启
verify.py       # 一次性验收脚本（compose verify 服务）
Dockerfile / docker-compose.yml
```
