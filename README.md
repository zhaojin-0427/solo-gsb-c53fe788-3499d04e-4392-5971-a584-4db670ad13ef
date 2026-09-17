# 🪝 Webhook 故障演练与回放台

一个完全本地运行的 Webhook **故障演练（chaos drill）+ 回放（replay）+ 分支对比** 工作台。

- **后端**：Python 3.11 / FastAPI / SQLite（WAL）
- **前端**：原生 HTML / CSS / JavaScript（无构建、无框架）
- **投递**：执行器发起**真实 HTTP 请求**到内置的 mock 接收端
- **驱动**：可暂停 / 可变速 / 可单步推进的**虚拟时钟**
- **可靠性**：稳定幂等键 + HMAC-SHA256 签名；刷新页面、重启容器后从持久化状态继续，且**绝不重复确认已确认投递**

---

## 一分钟启动（Docker Compose 一键）

```bash
docker compose up --build
```

打开浏览器访问：

- 控制台： **http://localhost:8000/**
- 健康检查： http://localhost:8000/api/health
- OpenAPI 文档： http://localhost:8000/docs
- 内置 mock 接收端基址： `http://localhost:8000/receiver`（场景内路径会拼在其后，如 `/receiver/hook`）

SQLite 数据持久化到宿主机的 `./data` 目录（compose 中已挂载卷）。停止 / 重启容器（`docker compose restart`、`docker compose down && docker compose up`）后，所有场景、演练、尝试、事件、已确认投递都会保留，并从断点继续。

停止：

```bash
docker compose down        # 保留 ./data
docker compose down -v     # 如需清空（这里没有命名卷，删除 ./data 即可）
```

---

## 不用 Docker，直接本地运行

需要 Python 3.11+。

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

默认数据库位于 `./data/webhook_lab.db`（首次自动建库建表）。

---

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `WH_DATA_DIR` | `./data`（Docker 内为 `/data`） | 数据目录 |
| `WH_DB_PATH` | `$WH_DATA_DIR/webhook_lab.db` | SQLite 文件完整路径 |
| `WH_TICK_MS` | `200` | 调度器扫描间隔（真实毫秒） |
| `WH_DEFAULT_SPEED` | `10` | 新演练的默认虚拟时钟倍率 |
| `WH_CLOCK_PUSH_MS` | `1000` | SSE 时钟刻度推送间隔 |
| `WH_RECEIVER_BASE_URL` | `http://127.0.0.1:8000` | 执行器访问接收端的基址（同进程部署无需修改） |
| `WH_REQUEST_TIMEOUT_SEC` | `2.5` | 投递的真实 HTTP 超时；“超时”规则会挂到此时长 |
| `WH_RECEIVER_HANG_SEC` | `30` | 接收端挂起保护上限 |

---

## 它能做什么

### 1. 设计故障场景
创建场景时可配置：

- **请求体**：任意 JSON
- **签名密钥**：每次投递带 GitHub 风格的 `X-Signature: sha256=<hmac>`，接收端按密钥与时间戳验签，错误签名返回 `401`
- **退避策略**：基础间隔、倍率、上限、最大尝试次数、可选（确定性）抖动
- **按尝试次数触发的规则**：`success`（2xx）、`status`（自定义状态码，如 500/429/201）、`timeout`（挂起连接直到客户端超时）、`disconnect`（发出半截响应后直接掐断连接），每条规则还可设置接收端处理延迟
- **默认动作**：未配置规则的尝试号使用的兜底行为

### 2. 用虚拟时钟驱动投递
在演练页可以：

- **暂停 / 恢复**：虚拟时钟完全冻结，投递与退避都不推进
- **变速**：如 1000× 快速跑完退避序列，或 0.0001× 慢速观察，可随时改倍率
- **单步推进**：暂停时 `+1ms` / `+1s`（也可 API 传任意 `advance_ms`），精确跨过某个计划时刻
- 页面顶部实时显示**虚拟时钟、下次尝试计划时刻、预计等待**

投递本身是真实 HTTP（超时会真实等待 `WH_REQUEST_TIMEOUT_SEC`），但真实请求耗时**不计入虚拟时间**——时钟只在投递间隙走动，保证退避序列严格按场景定义的虚拟毫秒发生，回放结果稳定可复现。

### 3. SSE 实时时间线
通过 `GET /api/events`（SSE，支持 `?after_id=<id>` 断线续传）实时推送：

`run_created` → `attempt_started` → `attempt_finished` → `attempt_scheduled` → … → `run_finished`，外加每秒一次的 `clock` 刻度；页面实时展示**每次尝试、下次计划和最终归宿**（`succeeded` / `exhausted`）。所有事件同时持久化在 SQLite。

### 4. 在任意检查点分叉，改规则后重放
每张已完成尝试卡片上都有 **“⑂ 从此检查点分叉重放”**：

- 继承检查点之前（含检查点）的**全部尝试与事件**（置灰标记“继承”，幂等键保持不变）
- 可覆盖后续尝试的故障规则与默认动作
- 子分支创建后处于暂停状态，可检查计划后再恢复重放
- 谱系（lineage）形如 `根 → 2 → 1`，每次分叉产生新的全局唯一根谱系
- 在 `#/compare` 页选择两条分支**并排比较**：逐次尝试的规则 / 结果 / HTTP 状态 / 耗时差异（自动高亮不同处）、事件序列差异、最终结局与副作用

### 5. 稳定幂等键与不重复确认
- 幂等键由 `sha256(根演练ID | 分叉谱系 | 尝试号 | 请求体哈希)` 确定性派生：同一分支同一尝试在任意时间、任意节点、重启前后得到**完全相同**的键
- 接收端仅在 2xx 时创建“业务副作用”，并以幂等键为唯一约束全局去重；同一键再次投递返回 `X-Replay: 1`，不再产生副作用
- “已确认投递”页（`#/effects`）列出全部副作用；崩溃恢复重投、手动重放、分叉继承都不会产生重复行
- 分叉点之后的尝试处于新谱系，键不同，是**全新的投递**，会正常产生新的副作用

---

## 崩溃 / 重启语义（投递一次且仅确认一次）

每个尝试的生命周期：`scheduled → running → done`。

1. 到期瞬间，调度器先在一个事务里把虚拟时钟对齐到计划时刻、把尝试落盘为 `running` 并提交，**然后**才发起真实 HTTP 请求；
2. 若进程在请求期间死亡，重启后该尝试仍是未完成状态，会被重新投递（at-least-once 传输）；
3. 接收端用幂等键去重：即使业务副作用已落盘而客户端没收到响应，重投也只会命中已存在记录并返回 `X-Replay: 1`，**已确认投递不重复**；
4. 调度器启动时会重新锚定所有 `running` 演练、保持 `paused` 演练冻结，从持久化的 `vt_now / anchor_ms / next_planned_ms` 继续。

---

## HTTP / API 速览

（另有完整交互式文档见 `/docs`）

```text
GET    /api/health
GET    /api/scenarios
POST   /api/scenarios
PUT    /api/scenarios/{id}
DELETE /api/scenarios/{id}
GET    /api/runs?scenario_id=...
POST   /api/scenarios/{id}/runs          body: {"speed": 10}
GET    /api/runs/{id}                    含 attempts / events / branches
POST   /api/runs/{id}/control             {"action":"pause|resume|speed|advance", ...}
POST   /api/runs/{id}/fork                {"checkpoint_attempt":2,"rules":[...],"label":"..."}
GET    /api/families/{root_run_id}        整棵分支树 + 副作用/投递计数
GET    /api/side-effects?root_run_id=...   已确认投递（业务副作用）
GET    /api/events?after_id=0              SSE
POST   /receiver/<url_path>                 内置 mock 接收端（执行器使用）
```

控制接口示例：

```bash
curl -XPOST localhost:8000/api/runs/$RUN_ID/control -d '{"action":"pause"}'
curl -XPOST localhost:8000/api/runs/$RUN_ID/control -d '{"action":"advance","advance_ms":1000}'
curl -XPOST localhost:8000/api/runs/$RUN_ID/control -d '{"action":"resume"}'
curl -XPOST localhost:8000/api/runs/$RUN_ID/control -d '{"action":"speed","speed":1000}'
```

---

## 一个建议的演练流程

1. 新建场景：第 1 次 `timeout`、第 2 次 `disconnect`、第 3 次 `status 500`，默认成功，退避 `200ms ×2`、最多 6 次
2. 以 1000× 开始演练，观察前 3 次失败 → 第 4 次 200 成功；副作用只有 1 条
3. 在检查点 #2 分叉，把第 3 次规则改为 `success` → 子分支第 3 次即成功
4. 打开“分支对比”，并排查看两条分支的尝试差异与事件序列
5. `docker compose restart` 后刷新：历史、暂停状态与已确认投递全部保留

---

## 目录结构

```
app/
  main.py         FastAPI 入口（REST + SSE + 静态资源 + 接收端挂载）
  scheduler.py     调度器：虚拟时钟、到期投递、退避、重启恢复
  execution.py     规则/退避计算、真实 HTTP 投递、签名头
  receiver.py      内置 mock 接收端（ASGI 子应用）
  runs.py          创建演练 / 检查点分叉 / 时钟控制
  db.py            SQLite 持久化层
  signing.py        幂等键派生、HMAC 签名/验签
  hub.py           持久化事件总线 + SSE 广播
  models.py         Pydantic 模型
  config.py         环境变量配置
  static/           原生 JS 前端（index.html / app.js / styles.css）
e2e_test.py        端到端冒烟测试（需先启动服务）
Dockerfile / docker-compose.yml / requirements.txt
```

## 自带端到端测试

服务启动后运行：

```bash
pip install httpx
WH_DATA_DIR=/tmp/wh-test python3 e2e_test.py
```

覆盖：超时 / 断连 / 状态码矩阵、退避时序、幂等键确定性、重放去重、坏签名 401、检查点分叉、谱系、终局分叉、暂停 / 单步 / 变速，以及重启后状态与副作用保持。
