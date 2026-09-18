# Webhook 故障演练与回放台

本地运行的 Webhook 投递故障演练、分叉重放与并排对比工具。后端 Python + FastAPI + SQLite，前端原生 JavaScript（无构建步骤），Docker Compose 一键启动。

可以模拟「支付回调连续超时 → 断连 → 503 → 恢复 200」这类真实故障链，观察退避重试、幂等去重与 HMAC 签名，并在**任意一次尝试之后分叉**，修改故障规则重放时间线，并排比较两条分支的最终归宿、状态码序列与事件时间线。

## 功能一览

- **场景管理**：请求体（JSON）、HMAC 签名密钥、退避策略（fixed / linear / exponential，可选确定性全抖动、延迟上限、最大尝试次数）、按尝试次数触发的故障规则（`超时` / `断连` / `返回任意状态码`；尝试号 `0` 为兜底默认规则）。
- **可暂停的虚拟时钟**：每条分支拥有独立虚拟时钟，可暂停 / 继续、调速度（1×–600×）、手动快进（+1s/+10s/+60s）。调度完全由虚拟时间驱动，确定且可复现。
- **稳定幂等键 + HMAC 签名**：每次尝试携带 `Idempotency-Key`（仅由投递种子与尝试号派生，重启后保持不变）与 `X-Webhook-Signature`（HMAC-SHA256，签名内容含幂等键、虚拟时间戳与规范化请求体）。
- **持久化与崩溃恢复**：全部状态写入 SQLite（WAL）。刷新页面、重启进程或容器后，暂停中的分支继续暂停、计划时刻原样保留；已确认（进入收件箱）的投递**绝不会被重复投递**——重发会在收件箱命中幂等键并标记为「幂等回放」。
- **分叉与重放**：在任意已完成尝试的检查点分叉；之前的尝试作为共享历史被继承（半透明显示、不再重新投递），之后按修改后的规则 / 退避 / 上限 / 请求体 / 密钥继续重放。分叉使用独立幂等键空间，不会污染父分支。
- **SSE 实时画面**：`/api/stream` 推送每次尝试、下次计划、终态、时钟变化与分叉事件；断线自动重连（`Last-Event-ID` 补帧）。页面实时显示虚拟时钟、下次尝试倒计时与最终归宿（已送达 / 已放弃 / 永久失败）。
- **并排比较**：两条分支并列展示时间线，顶部汇总新尝试数、最终归宿、状态码集合与总虚拟耗时，差异高亮。
- **内置接收端**：`POST /hooks/{scenario_id}` 演示接收方契约——按 `Idempotency-Key` 幂等、用 HMAC 校验签名；右下角「收件箱」可查看所有已确认投递。
- 首次启动自动写入一个「支付回调（示例）」场景（超时 → 断连 → 503 → 200，指数退避 + 全抖动）。

## 一键启动（Docker Compose）

```bash
docker compose up --build
```

启动后浏览器访问：

- **控制台**：http://localhost:8000/
- **健康检查**：http://localhost:8000/healthz
- **SSE 事件流**：http://localhost:8000/api/stream

SQLite 数据持久化在命名卷 `wh-data`（容器内 `/data/wh.db`），`docker compose down` 后数据仍在；如需清空：

```bash
docker compose down -v
```

## 本地直接运行（不用 Docker）

需要 Python 3.11+：

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
WH_DB_PATH=./data/wh.db .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## 配置

通过环境变量配置：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WH_DB_PATH` | `./data/wh.db`（容器内 `/data/wh.db`） | SQLite 数据库路径，父目录会自动创建 |

服务监听端口由 uvicorn 参数决定（Dockerfile / compose 中固定为 `8000`），可在 `docker-compose.yml` 的 `ports` 改宿主映射，如 `"18000:8000"`。

## 使用指引

1. 左侧选择或新建场景：填写请求体 JSON、密钥、退避策略，并为第 N 次尝试配置故障（尝试号 `0` 表示兜底规则）。
2. 点击「▶ 发起投递」立即按 10× 虚拟时钟运行；或「⏸ 发起（暂停）」后用 +1s/+10s 手动步进，精确观察每一次尝试。
3. 右侧时间线实时呈现：计划（退避多久）、尝试结果（超时 / 断连 / HTTP 码、幂等键、签名、详情）、最终归宿。
4. 分叉前请先暂停分支（或等待其到达终态），以保证检查点时刻确定。在某次尝试的条目上点「⑂ 从这里分叉」（或分支标题栏的「在此分叉」），修改规则后创建新分支，自动与父分支并排对比；可再用右上角下拉切换对比对象。
5. 「收件箱」查看接收端确认日志，验证幂等去重。

## HTTP API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/scenarios` | 场景列表 / 创建 |
| GET/PUT/DELETE | `/api/scenarios/{id}` | 场景详情 / 更新 / 删除（级联删除投递） |
| POST | `/api/deliveries` | 发起投递（`{scenario_id, paused, speed}`） |
| GET | `/api/deliveries?scenario_id=` | 投递列表（含分支快照） |
| GET/DELETE | `/api/deliveries/{id}` | 投递详情（全部分支、尝试、事件）/ 删除 |
| POST | `/api/branches/{id}/fork` | 检查点分叉重放 |
| POST | `/api/branches/{id}/pause` · `/resume` · `/speed` · `/jump` | 虚拟时钟控制 |
| GET | `/api/stream` | SSE（事件类型：`evt`、`clock`） |
| GET | `/api/inbox` | 接收端幂等收件箱 |
| POST | `/hooks/{scenario_id}` | 外部 Webhook 接收端（幂等 + HMAC 校验） |

签名约定（外部接收端）：

```
Idempotency-Key: <稳定键>
X-Webhook-Timestamp: <毫秒时间戳>
X-Webhook-Signature: HMAC_SHA256(secret, key + "\n" + timestamp + "\n" + raw_body)
```

## 设计要点

- **一次尝试 = 一个数据库事务**：模拟是瞬时的，尝试记录、收件箱写入、下一计划在同一事务提交，进程在任意时刻崩溃都不会留下「进行中」的半次投递。
- **虚拟时钟**：`vtime = base_vtime + (wall - base_wall) × speed`，暂停即冻结；重启后从 `base_*` 恢复，计划时刻为绝对虚拟时间，到期才触发。
- **确定性**：幂等键由 `SHA256(命名空间, 场景, 创建时刻, 随机种子, 分支路径, 尝试号)` 派生并持久化；全抖动也由幂等键哈希决定，重启 / 重放结果完全一致。
- **终态语义**：2xx 送达；408 / 429 / 5xx 可重试（耗尽后「已放弃」）；其余 4xx 立即「永久失败」；超时 / 断连视为可重试。

## 测试

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
```

覆盖：退避重试送达、次数耗尽、暂停 + 手动步进、分叉修改规则重放与键隔离、4xx 立即终止、外部接收端签名/幂等、确定性抖动，以及关闭执行器重开数据库后的「重启恢复且不重复投递」。

## 目录结构

```
app/
  main.py        # FastAPI 入口、生命周期、静态文件
  api.py         # REST / SSE / 外部接收端
  executor.py    # 虚拟时钟、调度泵、尝试执行、时钟控制、分叉
  core.py        # 幂等键、HMAC、规则匹配、退避
  models.py      # Pydantic 模型与规范化
  db.py          # SQLite 连接与建表
  bus.py         # SSE 进程内发布订阅
  seed.py        # 首次启动示例场景
  static/        # index.html / app.js / styles.css（原生 JS）
tests/           # pytest + httpx 端到端测试
docker-compose.yml · Dockerfile
```
