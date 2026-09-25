# Sealed Predictions — 封存并按时公开预测的 Commit-Reveal 服务

基于 **FastAPI + PostgreSQL** 的预测封存服务：先在截止前提交预测的承诺哈希（SHA-256），
进入揭示期后再公开原文与随机盐，服务端重算哈希校验，**仅首次匹配的揭示生效**，
并提供可核验的事件时间线。所有截止时间均按**服务端 UTC** 判定。

## 快速启动（一条命令）

```bash
docker compose up --build
```

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`GET http://localhost:8000/health`

依赖安装与建表均在启动时自动完成：Compose 启动 Postgres 16 与 API 两个容器，
API 等待数据库健康检查后启动，并在启动时自动核对/初始化数据表
（`events` / `commitments` / `reveals` / `deadline_extensions` / `settlements`）。
两个容器均带 healthcheck，`docker compose ps` 中状态变为 `healthy` 即就绪。

### 启动期数据库结构核对

`create_all` 只会建缺失的表、不会给已有表补列，因此启动时区分**空库**与**已有
应用表的库**，避免「结构不完整却报告就绪、事件接口随后 500」：

- **空库**（不存在任何应用表）：按当前模型一次性创建全部 5 张应用表；
- **已有应用表**：按 SQLAlchemy 模型逐表核对表是否存在、每张表是否包含模型中的
  全部必需列。**不补列、也不在半旧库上补建其它表**；
- 结构完整的旧库（含历史数据）核对通过后正常启动，**数据原样保留**；
- 发现缺表或缺列时 API **拒绝就绪**：`GET /health` 返回 `503`
  （`code=SCHEMA_NOT_READY`），响应中明确列出缺失的数据表名与每张表缺失的必需
  列名，其余接口同样统一返回 `503`；Compose 的 healthcheck 据此把 API 标记为
  `unhealthy`，启动日志（`docker compose logs api`）也会输出同样的缺失清单。
  此时需要由运维按新版本迁移/修正数据库结构后再启动，服务不会静默改写半旧库。

## 只读完整性巡检（`python -m app.integrity_audit`）

服务镜像内置只读巡检命令，用于离线核对持久化数据与承诺哈希方案、结算固化
计数是否一致。巡检复用服务的 `DATABASE_URL` 连接配置（`app/database.py`）
与承诺哈希算法（`app/hashing.py`），在**同一 PostgreSQL 一致性只读快照**
（单条 `REPEATABLE READ READ ONLY` 事务）中逐事件核对，只执行 SELECT、
事务以回滚结束，**全程不改写任何业务数据**：

- **揭示核对**：每条揭示关联的承诺必须存在且属于该事件；用揭示保存的
  原文与盐按承诺哈希方案重算，重算值须同时等于揭示保存的
  `recomputed_hash` 与承诺的 `commitment_hash`；
- **结算核对**：已结算事件按标准结果重算正确 / 错误 / 未揭示数（与结算
  写入时同一统计口径：揭示原文与标准结果逐字符精确匹配），与固化计数
  逐项比较。

### 容器内巡检用法

Compose 一键启动（`docker compose up --build`）后，在 API 容器中执行：

```bash
docker compose exec api python -m app.integrity_audit           # 单行 JSON 报告
docker compose exec api python -m app.integrity_audit --pretty  # 缩进 JSON 报告
echo $?                                                          # 0=无异常，1=有异常
```

本地直跑（需自行提供 `DATABASE_URL`）：

```bash
DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/predictions \
  python -m app.integrity_audit
```

### 报告格式与退出码

报告为机器可读 JSON（stdout）：`status`（`ok` / `anomalies`）、
`generated_at`、`hash_scheme`、`snapshot`（PostgreSQL 上为
`REPEATABLE READ READ ONLY`）、`checked`（各表核对的记录数）、
`anomaly_count` 与 `anomalies`。每条异常给出稳定问题代码 `code`、所属
`event_id`、异常记录 ID（`reveal_id` / `commitment_id` / `settlement_id`）
及存库值与重算值：

```json
{
  "status": "anomalies",
  "generated_at": "2030-01-03T00:00:00+00:00",
  "hash_scheme": "sha256(u64be(len(utf8(text))) || utf8(text) || utf8(salt))",
  "snapshot": "REPEATABLE READ READ ONLY",
  "checked": {"events": 1, "commitments": 3, "reveals": 2, "settlements": 1},
  "anomaly_count": 1,
  "anomalies": [
    {
      "code": "SETTLEMENT_UNREVEALED_COUNT_MISMATCH",
      "event_id": "<EVENT_ID>",
      "settlement_id": "<SETTLEMENT_ID>",
      "stored_count": 0,
      "recounted_count": 1,
      "message": "结算固化的未揭示数与按标准结果重算的值不一致"
    }
  ]
}
```

稳定问题代码：

| code | 含义 |
|------|------|
| `REVEAL_COMMITMENT_MISSING` | 揭示关联的承诺不存在 |
| `REVEAL_COMMITMENT_EVENT_MISMATCH` | 揭示关联的承诺属于其他事件 |
| `REVEAL_RECOMPUTED_HASH_MISMATCH` | 原文与盐重算值不等于揭示保存的 `recomputed_hash` |
| `REVEAL_COMMITMENT_HASH_MISMATCH` | 原文与盐重算值不等于承诺的 `commitment_hash` |
| `SETTLEMENT_CORRECT_COUNT_MISMATCH` | 结算固化的正确数与重算值不一致 |
| `SETTLEMENT_INCORRECT_COUNT_MISMATCH` | 结算固化的错误数与重算值不一致 |
| `SETTLEMENT_UNREVEALED_COUNT_MISMATCH` | 结算固化的未揭示数与重算值不一致 |

退出码：`0` 无异常；`1` 发现异常（报告仍输出到 stdout）；`2` 巡检自身
执行失败（如数据库不可达，错误 JSON 输出到 stderr）。巡检只报告不修复，
可直接对接监控告警（退出码非 0 即告警）。

## 承诺哈希方案

```
commitment = SHA-256( 原文字节长度的 8 字节大端整数 || 原文 UTF-8 字节 || 盐 UTF-8 字节 )
```

8 字节大端长度前缀用于消除「原文 + 盐」拼接歧义。可用服务端工具端点核对本地实现：

```bash
curl -s -X POST http://localhost:8000/utils/commitment \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# {"commitment_hash": "...", "hash_scheme": "sha256(u64be(len(utf8(text))) || utf8(text) || utf8(salt))"}
```

## 事件生命周期（服务端 UTC 判定）

| 阶段 | 时间区间 | 允许操作 |
|------|----------|----------|
| `commit` | 创建时刻 ≤ now < commit_deadline | 提交承诺 |
| `reveal` | commit_deadline ≤ now < reveal_deadline | 提交揭示 |
| `closed` | now ≥ reveal_deadline | 只读；提交永久结算、查询结算结果 |

边界语义：到达 commit_deadline 的瞬间提交通道关闭、揭示通道开启；到达 reveal_deadline 揭示通道关闭，事件进入 closed 阶段，此后可提交一次永久结算。

**同一请求、同一 UTC 时刻**：每个会写入的请求（创建事件、单条/批量提交承诺、
单条/批量揭示）在入口只读取**一次**服务端 UTC 时钟，该时刻同时用于阶段准入
判定与本次记录 `created_at` 的写入——准入判定与记录时间不可能分属两个阶段，
即使读取与落库之间真实时钟恰好跨过某个截止点，获准的记录也不会被记到下一
阶段。批量请求内**各项记录共用这同一时刻**（时间戳全部相等），各项冲突仍按
原语义**整体回滚**；准入未通过的请求一律拒绝且不落库。因此历史快照按
`created_at <= at` 回放、时间线按 `created_at` 排序时，记录所处阶段与它被
接受的阶段始终一致。

**揭示与截止后结算按同一把事件行锁串行化**：单条揭示、批量揭示与永久结算
都在事务入口先对事件行执行 `SELECT … FOR UPDATE` 取得**同一把事件行锁**，
并在**取得锁之后**才读取该请求唯一的一次服务端 UTC（同时用于阶段判定与
`created_at`）。由此消除截止边界上的交错失真——揭示在截止前通过检查、尚未
提交时，结算无法插入把该承诺计为未揭示：

- **揭示先取得锁**：它按揭示期内的那次读数获准，先提交再放锁；随后取得锁的
  结算在同一事务内统计得到这条已提交的合法揭示，固化计数与存库记录一致；
- **结算先取得锁**：截止后完成固化再放锁；随后取得锁的揭示读到的唯一次 UTC
  必已不早于 `reveal_deadline`，按 `409 REVEAL_PHASE_CLOSED` 拒绝、回滚且
  **不落库任何揭示**（批量揭示同样整批回滚）。

加锁顺序恒为「事件行 → 承诺行」（批量揭示在事件锁内再对命中的承诺加行锁），
不存在反向加锁路径，因此不会死锁；「批量揭示全批回滚」「结算仅永久确认一次」
语义不变。SQLite（本地测试）将 `FOR UPDATE` 降级为普通查询，PostgreSQL
（Compose 生产环境）上行锁与唯一约束共同保证上述并发正确性。


## API 一览

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/events` | 创建事件（commit_deadline 须在未来，reveal_deadline 须更晚） |
| GET | `/events` | 事件列表（含状态与计数） |
| GET | `/events/{id}` | 事件详情 |
| GET | `/events/{id}/snapshot?at=...` | 按指定带时区历史时刻重建事件视图（回放延长与计数） |
| POST | `/events/{id}/extend` | 受约束地延长两个截止时间（仅 commit 阶段，幂等） |
| POST | `/events/conflicts` | 批量事件的阶段窗口冲突分析（sweep-line） |
| POST | `/events/schedule` | 批量事件的阶段窗口资源排程（最少轨道确定性着色） |
| POST | `/events/commitments/reuse-audit` | 跨事件承诺复用审计：归一化哈希查重并标记「提交前已被他事件揭示」 |
| POST | `/events/{id}/commitments` | 提交承诺（64 位十六进制哈希） |
| POST | `/events/{id}/commitments/batch` | 原子批量提交 1～100 个承诺（单事务，冲突整体回滚） |
| GET | `/events/{id}/commitments` | 承诺列表（含是否已揭示） |
| GET | `/events/{id}/commitments/proof/root` | 提交截止后获取承诺集合 Merkle 根与承诺数量 |
| GET | `/events/{id}/commitments/{commitment_id}/proof` | 提交截止后获取指定承诺的集合成员证明 |
| POST | `/utils/merkle-proof/verify` | 本地严格校验成员证明（叶数据、路径层级、根） |
| POST | `/events/{id}/reveals` | 揭示：提交原文 + 盐，服务端重算校验 |
| POST | `/events/{id}/reveals/batch` | 原子批量揭示 1～100 组原文与盐（单事务，冲突整体回滚） |
| GET | `/events/{id}/timeline` | 可核验的事件时间线 |
| POST | `/events/{id}/settlement` | closed 阶段永久确认标准结果并结算（仅一次） |
| GET | `/events/{id}/settlement` | 查询结算状态与结果（未结算返回明确状态） |
| GET | `/events/{id}/interval-report` | 已结算事件的数值区间预测报告（只读，十进制精确覆盖判定与排名） |
| POST | `/events/probability-calibration-report` | 跨事件概率校准报告（只读，Decimal 精确 Brier 均分与十分箱校准汇总） |
| POST | `/utils/commitment` | 按约定方案计算承诺哈希 |
| GET | `/health` | 健康检查 |

## 完整使用示例

```bash
# 1. 创建事件（截止时间须携带时区）
curl -s -X POST http://localhost:8000/events \
  -H 'Content-Type: application/json' \
  -d '{"title": "我的预测", "commit_deadline": "2030-01-01T00:00:00Z", "reveal_deadline": "2030-01-02T00:00:00Z"}'
# => {"id": "<EVENT_ID>", "status": "commit", ...}

# 2. 本地计算承诺（Python 参考实现）
python3 - <<'EOF'
import hashlib
text, salt = "明天下雨", "my-random-salt"
tb = text.encode("utf-8")
print(hashlib.sha256(len(tb).to_bytes(8, "big") + tb + salt.encode()).hexdigest())
EOF

# 3. 提交截止前提交承诺
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hash": "<64位十六进制哈希>"}'

# 4. 进入揭示期后提交原文与盐
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals \
  -H 'Content-Type: application/json' \
  -d '{"original_text": "明天下雨", "salt": "my-random-salt"}'
# => {"verified": true, "recomputed_hash": "...", "commitment_hash": "...", ...}

# 5. 获取可核验时间线（含原文与盐，任何人可重算哈希比对）
curl -s http://localhost:8000/events/<EVENT_ID>/timeline

# 6. 进入 closed 阶段（now >= reveal_deadline）后提交非空标准结果，永久结算
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/settlement \
  -H 'Content-Type: application/json' \
  -d '{"standard_result": "明天下雨"}'
# => {"event_id": "...", "status": "settled", "standard_result": "明天下雨",
#     "correct_count": 1, "incorrect_count": 0, "unrevealed_count": 1,
#     "settled_at": "2030-01-02T00:00:01Z"}

# 7. 查询结算结果（未结算时 status 为 "unsettled"，其余字段为 null）
curl -s http://localhost:8000/events/<EVENT_ID>/settlement
```

## 受约束的截止时间延长（`POST /events/{id}/extend`）

仅在 **commit 阶段**可由调用方延长事件窗口。请求体携带新的两个截止时间（**必须带
时区**）与 `reason`，服务端在**单个事务**中完成全部校验与更新：

- 仅允许在当前 commit 阶段调用；事件进入 reveal / closed 阶段返回
  `409 EXTENSION_NOT_IN_COMMIT_PHASE`；
- 新 `commit_deadline` 必须**严格晚于**当前 commit 截止时间；
- 新 `reveal_deadline` 必须**严格晚于**新 `commit_deadline`，且不得早于当前 reveal
  截止时间 —— **两个截止时间只能延长，不能缩短，也不能保持不变**；
- 任何校验失败均回滚事务，**不改变截止时间**，也不写入任何记录；
- 响应同时返回旧值、新值与 `reason`；每次成功延长都会写入审计表
  `deadline_extensions`，并作为 `deadline_extended` 条目出现在事件时间线中
  （时间线里的 `event_created` 始终展示创建时的原始截止时间；同一时刻连续
  多次延长时，同刻记录按延长链顺序排列，即新截止时间严格递增的业务顺序）。

```bash
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/extend \
  -H 'Content-Type: application/json' \
  -d '{"commit_deadline": "2030-01-01T06:00:00Z",
       "reveal_deadline": "2030-01-02T06:00:00Z",
       "reason": "数据源延迟，顺延提交与揭示窗口"}'
```

```json
{
  "event_id": "<EVENT_ID>",
  "old_commit_deadline": "2030-01-01T00:00:00Z",
  "old_reveal_deadline": "2030-01-02T00:00:00Z",
  "new_commit_deadline": "2030-01-01T06:00:00Z",
  "new_reveal_deadline": "2030-01-02T06:00:00Z",
  "reason": "数据源延迟，顺延提交与揭示窗口",
  "extended_at": "2030-01-01T00:05:00Z"
}
```

**幂等重放**：对**最近一次成功延长**（即延长链链尾，按新截止时间严格
递增确定，与记录写入时刻及 UUID 无关）提交完全相同的请求（新
`commit_deadline`、新 `reveal_deadline` 与 `reason` 三者均一致，按 UTC
时刻比较），无论间隔多久、服务端时钟是否已推进，都直接返回首次延长的同一
结果，不重复写入。即使两次延长发生在**同一时刻**，最近一次仍按延长链
（新截止时间递增）识别，不会因记录 UUID 大小认错。仅时间相同而 `reason`
不同不算重放，按普通请求校验（若当前值已未再延长，则收到 409，状态不变）。

```bash
# 与上一次成功延长完全一致 -> 200，返回与首次完全相同的结果（幂等）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/extend \
  -H 'Content-Type: application/json' \
  -d '{"commit_deadline": "2030-01-01T06:00:00Z",
       "reveal_deadline": "2030-01-02T06:00:00Z",
       "reason": "数据源延迟，顺延提交与揭示窗口"}'
```

延长后可基于新窗口继续延长（连续延长时，旧值为上一次延长后的新值，链条完整记录在
审计表与时间线中）。失败语义：

| HTTP | code | 场景 |
|------|------|------|
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 409 | `EXTENSION_NOT_IN_COMMIT_PHASE` | 事件已进入 reveal / closed 阶段 |
| 409 | `INVALID_EXTENSION` | 新 commit 未晚于当前值，或新 reveal 未晚于新 commit / 当前 reveal（只延不缩） |
| 422 | — | 时间格式非法、时间无时区、`reason` 缺失或为空、事件 ID 非 UUID |

## 历史时刻事件快照（`GET /events/{id}/snapshot?at=...`）

按调用方指定的**带时区历史时刻**重建事件在当时的只读视图，与服务端当前时钟无关，
也不会修改任何数据。典型用途：审计「某个截止时间延长生效之前」事件处于什么阶段、
当时已有多少承诺与揭示。

```bash
curl -s 'http://localhost:8000/events/<EVENT_ID>/snapshot?at=2030-01-01T00:30:00Z'
```

```json
{
  "event_id": "<EVENT_ID>",
  "title": "我的预测",
  "description": null,
  "created_at": "2030-01-01T00:00:00Z",
  "snapshot_at": "2030-01-01T00:30:00Z",
  "commit_deadline": "2030-01-01T06:00:00Z",
  "reveal_deadline": "2030-01-02T06:00:00Z",
  "status": "commit",
  "commitment_count": 2,
  "reveal_count": 0
}
```

**回放规则**（所有比较按 UTC 时刻，边界均**含 `at` 本身**）：

- 以事件创建记录为起点，按延长链业务顺序回放
  `deadline_extensions`——延长链中每条成功延长的新截止时间严格递增，
  因此**按 `(new_commit_deadline, new_reveal_deadline)` 升序即业务发生
  先后**（同一时刻连续多次延长时也成立，不依赖记录 UUID 大小）；仅
  `created_at <= at` 的延长在该时刻已生效，**最近一条**已生效延长的新
  截止时间即当时生效的 `commit_deadline` / `reveal_deadline`；若首次
  延长尚未发生，则使用创建时的原始截止时间（即首条延长审计记录的旧值）；
- `status` 用**回放后**的截止时间按常规半开语义判定：`at < commit_deadline`
  为 `commit`，`at < reveal_deadline` 为 `reveal`，其后为 `closed`；
- `commitment_count` / `reveal_count` 只统计 `created_at <= at` 的记录
  （该时刻之后才写入的承诺/揭示不计入）；
- 响应回显 `created_at` 与归一化到 UTC 的 `snapshot_at`。

`at` 接受任意携带时区偏移的 ISO 8601 时间（`Z` 后缀与 `+08:00` 这类偏移均可，
按同一 UTC 时刻处理）。允许取**未来**时刻：返回截至查询时已知的最新截止时间、
全部已写入计数与按该时刻判定的状态。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 快照重建成功 |
| 400 | `MISSING_SNAPSHOT_TIME` | 缺少查询参数 `at` |
| 400 | `INVALID_SNAPSHOT_TIME` | `at` 为空或无法解析为 ISO 8601 时间 |
| 400 | `NAIVE_SNAPSHOT_TIME` | `at` 未携带时区偏移（如 `2030-01-01T00:30:00`） |
| 400 | `SNAPSHOT_BEFORE_CREATION` | `at` 早于事件创建时间（事件在该时刻尚不存在） |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 422 | — | 事件 ID 非 UUID |

## 事件窗口冲突分析（`POST /events/conflicts`）

把每个事件按**服务端 UTC** 视为两段**半开**时间区间，用 sweep-line 计算任意时刻
有哪些事件同时处于**同一阶段**（同为 `commit` 或同为 `reveal`，跨阶段不算冲突）：

```
commit: [created_at, commit_deadline)
reveal: [commit_deadline, reveal_deadline)
```

半开边界语义：到达 `commit_deadline` 的瞬间旧事件已离开 commit 阶段、新事件可恰好
进入——首尾相接（一个区间的右端点等于另一个的左端点）**不产生**冲突。返回的冲突片段
按 `start`、`end`、`phase` 排序，片段内 `event_ids` 升序；无冲突（或请求为空）时
稳定返回空列表。

```bash
curl -s -X POST http://localhost:8000/events/conflicts \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]}'
```

```json
{
  "generated_at": "2030-06-01T00:00:00Z",
  "phases": ["commit", "reveal"],
  "count": 2,
  "segments": [
    {
      "start": "2030-01-01T01:00:00Z",
      "end": "2030-01-01T02:00:00Z",
      "phase": "commit",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    },
    {
      "start": "2030-01-02T01:00:00Z",
      "end": "2030-01-02T02:00:00Z",
      "phase": "reveal",
      "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"]
    }
  ]
}
```

空结果同样稳定：`POST /events/conflicts -d '{"event_ids": []}'`（或省略字段）返回
`{"generated_at": "...", "phases": ["commit", "reveal"], "count": 0, "segments": []}`。
请求中的重复 ID 自动去重；任一 ID 不存在返回 `404 EVENT_NOT_FOUND`；非 UUID 返回 422。

## 事件窗口资源排程（`POST /events/schedule`）

为若干事件的两阶段半开窗口安排 **track（轨道）**：同一 track、同一阶段的两个窗口
不得重叠；commit 与 reveal **分别独立着色**（两套轨道编号互不影响，均从 1 开始）。
窗口定义与半开语义同冲突分析：

```
commit: [created_at, commit_deadline)
reveal: [commit_deadline, reveal_deadline)
```

首尾相接（前一窗口右端点恰为后一窗口左端点）不重叠，**可复用同一 track**。排程按
`(起点, 终点, event_id)` 升序做确定性贪心着色（结束窗口先离场、再取编号最小的空闲
track），所用 track 数等于该阶段的最大并发深度，即**最小所需轨道数**；响应中的
`required_tracks` 取 commit / reveal 两阶段的较大值。请求体携带去重后的事件 ID 与
可用 track 数 `track_limit`（>= 1 的整数）：

```bash
curl -s -X POST http://localhost:8000/events/schedule \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>", "<EVENT_ID_3>"],
       "track_limit": 2}'
```

```json
{
  "track_limit": 2,
  "required_tracks": 3,
  "feasible": false,
  "assignments": [
    {"event_id": "<EVENT_ID_1>", "commit_track": 1, "reveal_track": 1},
    {"event_id": "<EVENT_ID_2>", "commit_track": 2, "reveal_track": 2},
    {"event_id": "<EVENT_ID_3>", "commit_track": 3, "reveal_track": 3}
  ],
  "conflicts": [
    {"phase": "commit", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_2>"},
    {"phase": "commit", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "commit", "event_id_1": "<EVENT_ID_2>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_2>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_1>", "event_id_2": "<EVENT_ID_3>"},
    {"phase": "reveal", "event_id_1": "<EVENT_ID_2>", "event_id_2": "<EVENT_ID_3>"}
  ]
}
```

字段与语义：

- `required_tracks`：排下所有窗口所需的最小 track 数（两阶段最大并发深度的较大值）；
- `feasible`：`required_tracks <= track_limit` 时为 `true`，HTTP 状态码始终为 200；
- `assignments`：按 `event_id` 升序返回每个事件的 `commit_track` / `reveal_track`
  （1 起编号；零长度阶段窗口不占任何时刻，对应编号为 `null`）；
- `conflicts`：`feasible=false` 时给出导致超出上限的冲突事件对——即某阶段并发窗口数
  **严格多于** `track_limit` 的时间片段内同时活动的每一对事件（同阶段、半开重叠；
  跨片段去重，按 `phase`、`event_id_1`、`event_id_2` 排序，对内 ID 升序）；
  `feasible=true` 时恒为空数组。注意：仅普通两两重叠、但从未超过上限的事件对
  **不在**此列（上限内排得下即不算阻塞冲突）。

确定性与只读：响应只取决于各事件当前的窗口与 `track_limit`，不含任何时间戳字段，
**相同输入重复请求（含打乱/重复 ID 的等价输入）结果逐字节一致**；排程不写入数据库，
不修改任何事件数据。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 排程成功（`feasible=false` 同样返回 200，冲突见 `conflicts`） |
| 400 | `INVALID_TRACK_LIMIT` | `track_limit` 缺失、为 null、不是整数（含布尔值）或小于 1 |
| 404 | `EVENT_NOT_FOUND` | 请求中的任一事件 ID 不存在 |
| 422 | — | 事件 ID 非 UUID 或请求体结构错误 |

## 跨事件承诺复用审计（`POST /events/commitments/reuse-audit`）

在多个事件之间审计同一承诺哈希的复用情况，帮助发现「预测内容在 A 事件中已被公开、
随后又被复用到 B 事件」的可疑行为。接口为**纯只读**，不改变任何提交、揭示与结算。

请求体携带 **2～50 个**事件 ID（重复 ID 自动去重）。服务把各事件承诺的
`commitment_hash` 按**大小写归一**（小写）后分组，仅当同一哈希出现在
**>= 2 个不同事件**时列为重复项。每个重复项返回所属事件、承诺 ID 与提交时间，
并逐项判定「提交前是否已被其他事件公开」：

- 仅当**其他事件**中同哈希承诺存在揭示、且该揭示时间**严格早于**本项提交时间时，
  才标记 `exposed_before_commit=true`，并在 `prior_reveal_sources` 中列出符合条件
  的揭示来源（`event_id` / `commitment_id` / `reveal_id` / `revealed_at`）；
- **同一时刻揭示不算事先公开**（必须严格早于）；同一事件自身的揭示也不计为来源；
  不符合条件时 `exposed_before_commit=false`、来源列表为空；
- 结果按 `(commitment_hash, committed_at, commitment_id)` **稳定排序**；
  来源按 `(revealed_at, event_id, reveal_id)` 排序；
- 审计范围仅限请求中的事件：未列入请求的事件即使有同哈希揭示也不作为来源。

```bash
curl -s -X POST http://localhost:8000/events/commitments/reuse-audit \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>", "<EVENT_ID_3>"]}'
```

```json
{
  "generated_at": "2030-07-01T12:00:00Z",
  "event_count": 3,
  "duplicate_hash_count": 1,
  "item_count": 3,
  "items": [
    {
      "commitment_hash": "9f2c7fd2…",
      "event_id": "<EVENT_ID_1>",
      "commitment_id": "<COMMITMENT_ID_1>",
      "committed_at": "2030-06-01T00:00:00Z",
      "exposed_before_commit": false,
      "prior_reveal_sources": []
    },
    {
      "commitment_hash": "9f2c7fd2…",
      "event_id": "<EVENT_ID_2>",
      "commitment_id": "<COMMITMENT_ID_2>",
      "committed_at": "2030-06-05T00:00:00Z",
      "exposed_before_commit": true,
      "prior_reveal_sources": [
        {
          "event_id": "<EVENT_ID_1>",
          "commitment_id": "<COMMITMENT_ID_1>",
          "reveal_id": "<REVEAL_ID_1>",
          "revealed_at": "2030-06-03T00:00:00Z"
        }
      ]
    },
    {
      "commitment_hash": "9f2c7fd2…",
      "event_id": "<EVENT_ID_3>",
      "commitment_id": "<COMMITMENT_ID_3>",
      "committed_at": "2030-06-10T00:00:00Z",
      "exposed_before_commit": true,
      "prior_reveal_sources": [
        {"event_id": "<EVENT_ID_1>", "commitment_id": "<COMMITMENT_ID_1>",
         "reveal_id": "<REVEAL_ID_1>", "revealed_at": "2030-06-03T00:00:00Z"},
        {"event_id": "<EVENT_ID_2>", "commitment_id": "<COMMITMENT_ID_2>",
         "reveal_id": "<REVEAL_ID_2>", "revealed_at": "2030-06-08T00:00:00Z"}
      ]
    }
  ]
}
```

无跨事件重复时稳定返回空列表：`duplicate_hash_count=0`、`item_count=0`、`items=[]`
（只在单个事件内出现的哈希，无论是否已揭示，均不列入）。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 审计完成（无重复时 `items` 为空） |
| 404 | `EVENT_NOT_FOUND` | 请求中的任一事件 ID 不存在（先去重再校验） |
| 422 | — | 事件 ID 数量不在 2～50、ID 非 UUID 或请求体结构错误 |

## 原子批量提交承诺（`POST /events/{id}/commitments/batch`）

在**单个数据库事务**中按输入顺序提交 1～100 个承诺哈希。服务端在写入前完成全部校验，
**任何一项冲突都整体回滚**，不会留下部分承诺：

- 逐项校验哈希格式（64 位十六进制，大小写均可，存储时归一化为小写）；
- 检查请求内重复（第二次及以后出现的下标记为请求内重复，大小写不敏感）；
- 一次性查询数据库中该事件已有的哈希。

冲突时返回 `409 BATCH_COMMITMENT_CONFLICT`，统一错误结构内附 `conflicts` 数组，
每项给出 `index`（从 0 开始的输入下标）、`type` 与提交时的原始哈希，按下标升序、
每个下标至多一项：

| 冲突 type | 含义 |
|-----------|------|
| `INVALID_FORMAT` | 不是 64 位十六进制哈希 |
| `DUPLICATE_IN_REQUEST` | 同一哈希在本次请求中重复（首次出现之后的下标） |
| `DUPLICATE_COMMITMENT` | 数据库中该事件已存在此哈希（请求内首次出现的下标） |

```bash
# 成功：按输入顺序创建，返回 count 与承诺记录（201）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
      ]}'
# => {"count": 2, "commitments": [{...}, {...}]}

# 冲突：整体回滚（409），逐项指出冲突下标与类型
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/commitments/batch \
  -H 'Content-Type: application/json' \
  -d '{"commitment_hashes": [
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d",
        "not-a-valid-hash",
        "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"
      ]}'
```

```json
{
  "detail": {
    "code": "BATCH_COMMITMENT_CONFLICT",
    "message": "批量提交包含 2 个冲突项，已整体回滚，未创建任何承诺",
    "conflicts": [
      {"index": 1, "type": "INVALID_FORMAT", "commitment_hash": "not-a-valid-hash"},
      {"index": 2, "type": "DUPLICATE_IN_REQUEST",
       "commitment_hash": "9f2c7fd25e1b3afad3e85a0bd17d9b100db4b3bd306a4f6e5535e23e15e82d7d"}
    ]
  }
}
```

列表为空或超过 100 项返回 422；事件不存在返回 `404 EVENT_NOT_FOUND`；
已过 `commit_deadline` 与单个提交端点一致返回 `409 COMMIT_PHASE_CLOSED`，
以上情形同样不产生任何写入。整批的阶段判定与各条承诺 `created_at` 取自
**请求入口的同一 UTC 时刻**（详见上文「事件生命周期」），因此返回记录的
提交时间全部相等，且不会出现「截止前获准、记录却落在揭示期」的情况。

## 原子批量揭示（`POST /events/{id}/reveals/batch`）

进入揭示期后，可在**单个数据库事务**中按输入顺序提交 1～100 组「原文 + 盐」。
服务端按约定方案逐项重算哈希并与该事件的承诺匹配，**所有项目先完成全部校验，
任何一项冲突都整体回滚**，不会留下部分揭示：

- 逐项校验格式（盐须为 1～256 字符的字符串）；
- 按重算哈希检查请求内重复（第二次及以后出现的下标记为请求内重复）；
- 一次性查询命中的承诺（并对这些承诺加行锁，串行化并发批量揭示），判定
  「无匹配承诺」与「承诺已揭示」；
- 仅在揭示期内可调用：提前揭示返回 `409 REVEAL_PHASE_NOT_STARTED`，
  逾期揭示返回 `409 REVEAL_PHASE_CLOSED`（与单个揭示端点一致）。

冲突时返回 `409 BATCH_REVEAL_CONFLICT`，统一错误结构内附 `conflicts` 数组，
每项给出 `index`（从 0 开始的输入下标）与 `type`，按下标升序、每个下标至多
一项；除格式非法项外均附带 `recomputed_hash`，每项都回显提交时的原文与盐：

| 冲突 type | 含义 |
|-----------|------|
| `INVALID_FORMAT` | 盐为空或超过 256 字符（原文允许任意 UTF-8 文本，含空串） |
| `DUPLICATE_IN_REQUEST` | 重算哈希在本次请求中重复（首次出现之后的下标） |
| `NO_MATCHING_COMMITMENT` | 重算哈希不匹配本事件中的任何承诺 |
| `DUPLICATE_REVEAL` | 匹配到的承诺此前已被揭示（请求内首次出现的下标） |

```bash
# 成功：揭示期内按输入顺序一次事务写入，返回 count 与揭示记录（201）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals/batch \
  -H 'Content-Type: application/json' \
  -d '{"reveals": [
        {"original_text": "明天下雨", "salt": "my-random-salt-1"},
        {"original_text": "明年晴天", "salt": "my-random-salt-2"}
      ]}'
# => {"count": 2, "reveals": [{...verified: true...}, {...}]}

# 冲突：整体回滚（409），逐项指出冲突下标与类型
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/reveals/batch \
  -H 'Content-Type: application/json' \
  -d '{"reveals": [
        {"original_text": "明天下雨", "salt": "my-random-salt-1"},
        {"original_text": "错误原文", "salt": "wrong-salt"},
        {"original_text": "明天下雨", "salt": "my-random-salt-1"}
      ]}'
```

```json
{
  "detail": {
    "code": "BATCH_REVEAL_CONFLICT",
    "message": "批量揭示包含 3 个冲突项，已整体回滚，未创建任何揭示",
    "conflicts": [
      {"index": 0, "type": "DUPLICATE_REVEAL",
       "original_text": "明天下雨", "salt": "my-random-salt-1",
       "recomputed_hash": "…"},
      {"index": 1, "type": "NO_MATCHING_COMMITMENT",
       "original_text": "错误原文", "salt": "wrong-salt",
       "recomputed_hash": "…"},
      {"index": 2, "type": "DUPLICATE_IN_REQUEST",
       "original_text": "明天下雨", "salt": "my-random-salt-1",
       "recomputed_hash": "…"}
    ]
  }
}
```

列表为空或超过 100 项、请求体结构错误（如项不是对象、缺少 `original_text`
或 `salt`、字段类型错误）返回 422；事件不存在返回 `404 EVENT_NOT_FOUND`，
以上情形均不产生任何写入。批量端点与单个揭示端点共享同一套存储与唯一约束：
**并发下仅首次揭示生效**——两个揭示入口与永久结算先共用**同一把事件行锁**
串行（取得事件锁后才做那唯一次阶段读数），预检阶段在事件锁内再对命中的承诺
加行锁（PostgreSQL），提交阶段由 `reveals.commitment_id` 唯一约束兜底，
竞争落败的整批请求回滚并收到逐项 `DUPLICATE_REVEAL` 清单；截止后先完成的
结算持锁期间揭示排队，放锁后揭示按 `REVEAL_PHASE_CLOSED` 整批拒绝、不落库，
反之先提交的合法揭示必被随后的结算统计。整批的阶段判定与各条揭示 `created_at`
取自**取得事件锁后的同一 UTC 时刻**（详见上文「事件生命周期」），返回记录的
揭示时间全部相等，且不会出现「揭示期内获准、记录却落在 closed 阶段」的情况。

## 事件承诺集合证明（Merkle）

提交截止后承诺集合即冻结。服务提供事件承诺集合的 **Merkle 根**与指定承诺的
**成员证明**，调用方无需下载完整时间线即可验证「某个承诺确实进入了该事件」。
证明为纯只读接口，不影响提交、揭示与结算流程。

### 哈希与树构造规则

- **叶节点按承诺 ID（UUID 128 位整数）升序排列**，与提交先后无关；
- 叶哈希同时绑定**事件 ID、承诺 ID 与原有 commitment_hash**（UUID 取 16 字节
  大端，承诺哈希取 32 字节），叶带独立域前缀，无法把其他事件 / 其他位置的
  节点挪作他用：

  ```
  leaf = SHA-256( b"sealed-predictions:leaf:v1:"
                  || uuid128be(event_id) || uuid128be(commitment_id)
                  || commitment_hash_bytes )
  ```

- **内部节点与叶节点使用不同前缀**（域分离）：

  ```
  node = SHA-256( b"sealed-predictions:node:v1:" || left || right )
  ```

- 每层节点数为奇数时，**末项复制自身后配对**；因此单叶集合的根就是叶哈希本身
  （零次内部哈希）；
- **空集合**不经过叶 / 内部构造，返回固定空根
  `SHA-256(b"sealed-predictions:empty:v1:")`（十六进制
  `6d25267464b4d9156c5ba4e7dcd9d6d90d4b4cd4ca3fb6ba9ed25e02042097fe`）与
  数量 `0`，与任何真实集合的根都不相同。

### 根与数量：`GET /events/{id}/commitments/proof/root`

仅在 **提交截止后**（`now >= commit_deadline`，reveal / closed 阶段均可）签发；
未过提交截止时集合仍在变化，返回 `409 PROOF_NOT_AVAILABLE`。

```bash
curl -s http://localhost:8000/events/<EVENT_ID>/commitments/proof/root
```

```json
{
  "event_id": "<EVENT_ID>",
  "merkle_root": "d1438805…",
  "empty_root": "6d25267464b4d9156c5ba4e7dcd9d6d90d4b4cd4ca3fb6ba9ed25e02042097fe",
  "commitment_count": 5,
  "tree_scheme": "merkle-sha256-v1: …",
  "leaf_scheme": "sha256('sealed-predictions:leaf:v1:' || …)",
  "generated_at": "2030-01-01T00:00:00Z"
}
```

零承诺事件同样返回 200：`commitment_count=0`、`merkle_root` 等于 `empty_root`。

### 成员证明：`GET /events/{id}/commitments/{commitment_id}/proof`

提交截止后为指定承诺签发证明，给出叶哈希、叶下标、叶总数、根与自叶向根的
逐层兄弟哈希及左右位置。`position` 表示**兄弟相对当前节点**的位置：
`left` = 兄弟在左（当前节点在右，父 = H(兄弟 ‖ 当前)），
`right` = 兄弟在右（父 = H(当前 ‖ 兄弟)）；奇数层末项复制时，复制出的兄弟
标记为 `left` 且其哈希等于当前节点。

```bash
curl -s http://localhost:8000/events/<EVENT_ID>/commitments/<COMMITMENT_ID>/proof
```

```json
{
  "event_id": "<EVENT_ID>",
  "commitment_id": "<COMMITMENT_ID>",
  "commitment_hash": "9f2c7fd2…",
  "leaf_hash": "2a7f…",
  "leaf_index": 3,
  "leaf_count": 5,
  "merkle_root": "d1438805…",
  "proof": [
    {"sibling": "…", "position": "left"},
    {"sibling": "…", "position": "right"},
    {"sibling": "…", "position": "left"}
  ],
  "tree_scheme": "merkle-sha256-v1: …",
  "leaf_scheme": "sha256('sealed-predictions:leaf:v1:' || …)",
  "generated_at": "2030-01-01T00:00:00Z"
}
```

### 本地严格校验：`POST /utils/merkle-proof/verify`

纯计算接口（不查询数据库、不做阶段判定），对**叶数据、路径与根**做严格校验：

1. 按叶方案用 `event_id` + `commitment_id` + `commitment_hash` 重算叶哈希；
2. 依据 `leaf_count` / `leaf_index` 重放每层配对结构（含奇数层末项复制），
   逐层核对路径长度（必须恰好收敛到根）、每层兄弟左右位置、复制层兄弟必须
   等于当前节点；
3. 重算根并与 `merkle_root` 逐字节比较。

全部通过返回 HTTP 200 与 `valid: true`；结构或根不符同样返回 200 与
`valid: false` 及明确 `reason`（调用方据此区分拒绝原因）；哈希非 64 位
十六进制、位置非 `left/right`、下标为负、`leaf_count<1`、路径超过 64 层等
**请求体格式问题返回 422**。`commitment_hash` 大写会归一化为小写。

```bash
curl -s -X POST http://localhost:8000/utils/merkle-proof/verify \
  -H 'Content-Type: application/json' \
  -d '{"event_id": "<EVENT_ID>", "commitment_id": "<COMMITMENT_ID>",
       "commitment_hash": "9f2c7fd2…", "leaf_index": 3, "leaf_count": 5,
       "merkle_root": "d1438805…",
       "proof": [{"sibling": "…", "position": "left"}, …]}'
# 通过 => {"valid": true, "reason": null, "leaf_hash": "…",
#          "computed_root": "d1438805…", "leaf_scheme": "…"}
```

| `valid=false` 的 reason | 含义 |
|------|------|
| `LEAF_INDEX_OUT_OF_RANGE` | `leaf_index` 越界（`leaf_count<1` 或下标不在 `[0, leaf_count)`） |
| `PATH_TOO_LONG` | 已收敛到根却仍有证明步骤（或路径超过 64 层） |
| `PATH_TOO_SHORT` | 步骤耗尽时尚未收敛到单根 |
| `UNEXPECTED_POSITION` | 某层兄弟的左右位置与该层下标奇偶性不符 |
| `DUPLICATION_MISMATCH` | 奇数层末项复制时兄弟不等于当前节点 / 位置不为 left |
| `ROOT_MISMATCH` | 叶数据或兄弟被篡改，重算根与提供的根不一致（响应含 `computed_root` 便于排查） |

证明端点与校验接口的错误：

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 根 / 证明签发成功；本地校验请求格式合法（`valid` 表示校验结果） |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 404 | `COMMITMENT_NOT_FOUND` | 承诺不存在，或不属于该事件 |
| 409 | `PROOF_NOT_AVAILABLE` | 未过提交截止（含截止时间被延长后、新截止未到） |
| 422 | — | 事件 / 承诺 ID 非 UUID；校验请求体格式错误 |

## 永久结算（`POST` / `GET /events/{id}/settlement`）

事件进入 **closed 阶段**（`now >= reveal_deadline`）后，调用方可提交一个
**非空**标准结果，服务在单个事务中完成判定并**永久确认**结算；在这之前
（commit / reveal 阶段）提交结算返回 `409 SETTLEMENT_NOT_IN_CLOSED_PHASE`。

服务按每条**已接受揭示**的 `original_text` 与标准结果做**逐字符精确匹配**
（区分大小写、区分空白，不做任何 trim、归一化或模糊比较），并在写入时固化
三个计数，三者之和等于事件的承诺总数：

| 计数 | 含义 |
|------|------|
| `correct_count` | 已揭示且 `original_text == standard_result` 的承诺数 |
| `incorrect_count` | 已揭示但原文与标准结果不相等的承诺数 |
| `unrevealed_count` | 承诺存在但从未揭示（含逾期未能揭示）的数量 |

**与揭示按同一把事件行锁串行（截止边界不失真）**：结算与单条 / 批量揭示
都先取得事件行的 `SELECT … FOR UPDATE` 锁，再读唯一一次服务端 UTC 并在同一
事务内完成统计与写入。因此揭示在截止前通过检查却尚未提交时，结算无法抢先
把该承诺固化为未揭示——揭示先持锁则它先提交并被结算计入；结算先持锁（截止后）
则其后取得锁的揭示读到的时钟必已 closed，被 `409 REVEAL_PHASE_CLOSED` 拒绝且
不落库。固化的三个计数与 `reveals` 表存库记录始终一致。

```bash
# closed 阶段提交标准结果（200；计数在确认时固化）
curl -s -X POST http://localhost:8000/events/<EVENT_ID>/settlement \
  -H 'Content-Type: application/json' \
  -d '{"standard_result": "明天下雨"}'
```

```json
{
  "event_id": "<EVENT_ID>",
  "status": "settled",
  "standard_result": "明天下雨",
  "correct_count": 1,
  "incorrect_count": 1,
  "unrevealed_count": 1,
  "settled_at": "2030-01-02T00:00:01Z"
}
```

结算一经确认**永久有效、不可改写**：对同一事件再次提交结算（无论标准结果
是否相同、是否处于同一请求）一律返回 `409 SETTLEMENT_ALREADY_CONFIRMED`，
统一错误结构的 `detail.conflicts[0].existing` 回显**首次**结算的完整结果，
数据库中的标准结果与三个计数保持不变；并发下由 `settlements.event_id`
唯一约束兜底，仅首次确认生效。

```json
{
  "detail": {
    "code": "SETTLEMENT_ALREADY_CONFIRMED",
    "message": "事件 ... 已结算，结算结果永久确认，重复结算被拒绝且不会改写既有结果",
    "conflicts": [
      {"existing": {"event_id": "<EVENT_ID>", "status": "settled",
                    "standard_result": "明天下雨",
                    "correct_count": 1, "incorrect_count": 1,
                    "unrevealed_count": 1,
                    "settled_at": "2030-01-02T00:00:01Z"}}
    ]
  }
}
```

**结算查询**（`GET`，纯只读，任何阶段均可调用）在未结算时返回 HTTP 200 与
明确的 `status: "unsettled"`，标准结果、三个计数与确认时间均为 `null`；
已结算时返回 `status: "settled"` 与永久结果：

```bash
curl -s http://localhost:8000/events/<EVENT_ID>/settlement
# 未结算 =>
# {"event_id": "<EVENT_ID>", "status": "unsettled",
#  "standard_result": null, "correct_count": null,
#  "incorrect_count": null, "unrevealed_count": null, "settled_at": null}
```

结算确认后还会作为 `settlement_confirmed` 条目（含标准结果与三个计数）
追加到事件时间线末尾。零承诺事件同样可以结算，三个计数均为 0。

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 查询成功（未结算时 `status="unsettled"`）；结算确认成功时 `status="settled"` |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 409 | `SETTLEMENT_NOT_IN_CLOSED_PHASE` | 事件仍处于 commit / reveal 阶段，尚未进入 closed |
| 409 | `SETTLEMENT_ALREADY_CONFIRMED` | 事件已永久结算，重复结算被拒绝（`detail.conflicts[0].existing` 回显首次结果） |
| 422 | — | `standard_result` 缺失、为空串或不是字符串；事件 ID 非 UUID |

## 数值区间预测报告（`GET /events/{id}/interval-report`）

对**已结算**事件生成只读的数值区间预测报告：把每条已揭示原文按
「`下界..上界`」解析为两个**有限十进制数**，用 `decimal.Decimal` 精确判定
区间是否覆盖结算标准结果，并对命中者排名。接口不修改任何数据，既有结算
计数（`correct_count` / `incorrect_count` / `unrevealed_count`）原样回显、
保持不变。

**区间文本语法**（严格匹配，不允许空白、下划线、`Infinity`/`NaN` 等宽松形式）：

```
interval       := finite_decimal ".." finite_decimal
finite_decimal := [+-]? ( 数字+ ["." 数字*] | "." 数字+ ) [ (e|E) [+-]? 数字+ ]
```

- 原文须恰好含一个 `..` 分隔符，两侧均为有限十进制数（如 `1..2`、
  `-1.5..2.5e1`）；**下界不得大于上界**；
- 解析失败的揭示单独列入 `invalid` 并注明 `reason`
  （`INVALID_FORMAT` / `LOWER_BOUND_EXCEEDS_UPPER`）；
- 从未揭示的承诺不参与解析，单独计入 `unrevealed_commitment_count`；
- 结算标准结果本身不是有限十进制数时，整个报告返回
  `422 STANDARD_RESULT_NOT_FINITE_DECIMAL`。

**覆盖判定与排名**（全程十进制精确计算，不经过浮点）：

- 闭区间覆盖：`下界 <= 标准结果 <= 上界`（边界命中算覆盖；`1.10` 与 `1.1`
  数值相等）；
- 区间宽度 `width = 上界 - 下界`，用**整数系数对齐运算精确求得**，不受
  Decimal 默认上下文 28 位精度影响（如 `1.4..1.6` 的宽度是 `0.2` 而非
  浮点近似；`0..0.123456789012345678901234567891` 与
  `0..0.123456789012345678901234567890` 的宽度末位不同，不会塌缩为
  同一个 28 位值）；结果的指数取两端点指数的较小值，故 `2.0..2.00`
  的宽度回显为 `0.00`；
- **宽度位数上限 4096**：某条合法区间的精确宽度需要超过 4096 位有效
  数字时（含 `1E-999999999999999999..1E+999999999999999999` 这类指数
  跨度过大、精确值无法安全物化的情形），整份报告直接返回
  `422 INTERVAL_WIDTH_TOO_LARGE`，**不静默舍入、不丢末位，也不会 500**。
  限制针对的是结果宽度：端点本身很长但相减后抵消（如两个 5000 位整数
  只差 1）时宽度仍正常精确给出。指数记法本身不占位数（`0..1E+5000`
  的精确宽度为 1 后接 5000 个零，共 5001 位，同样 422）；
- **命中者**按 `(真实区间宽度, 承诺提交时间, 承诺 ID)` 升序排名，`rank` 从 1 开始；
- **未命中者**列入 `misses` 但不排名（`rank` 为 `null`），按
  `(承诺提交时间, 承诺 ID)` 稳定排序。

```bash
curl -s http://localhost:8000/events/<EVENT_ID>/interval-report
```

```json
{
  "event_id": "<EVENT_ID>",
  "generated_at": "2030-01-02T00:00:05Z",
  "standard_result": "1.5",
  "settled_at": "2030-01-02T00:00:01Z",
  "correct_count": 0,
  "incorrect_count": 3,
  "unrevealed_count": 1,
  "commitment_count": 4,
  "revealed_count": 3,
  "unrevealed_commitment_count": 1,
  "valid_interval_count": 2,
  "hit_count": 2,
  "miss_count": 0,
  "invalid_count": 1,
  "hits": [
    {"commitment_id": "<CID_1>", "committed_at": "2030-01-01T00:10:00Z",
     "lower_bound": "1.4", "upper_bound": "1.6", "width": "0.2",
     "hit": true, "rank": 1},
    {"commitment_id": "<CID_2>", "committed_at": "2030-01-01T00:05:00Z",
     "lower_bound": "1", "upper_bound": "2", "width": "1",
     "hit": true, "rank": 2}
  ],
  "misses": [],
  "invalid": [
    {"commitment_id": "<CID_3>", "committed_at": "2030-01-01T00:20:00Z",
     "original_text": "not-an-interval", "reason": "INVALID_FORMAT"}
  ]
}
```

| HTTP | code | 场景 |
|------|------|------|
| 200 | — | 报告生成成功（纯只读，重复请求结果一致） |
| 404 | `EVENT_NOT_FOUND` | 事件不存在 |
| 409 | `INTERVAL_REPORT_NOT_SETTLED` | 事件尚未结算（含 closed 但未结算） |
| 422 | `STANDARD_RESULT_NOT_FINITE_DECIMAL` | 结算标准结果不是有限十进制数 |
| 422 | `INTERVAL_WIDTH_TOO_LARGE` | 存在精确宽度有效数字位数超过 4096 位的合法区间（不静默舍入） |
| 422 | — | 事件 ID 非 UUID |

## 跨事件概率校准报告（`POST /events/probability-calibration-report`）

对 **1～100 个已结算的二元事件**生成只读的跨事件概率校准报告：把每个事件的
已揭示原文当作预测概率，以结算标准结果（精确的 `"0"` / `"1"`）为实际结果，
用 `decimal.Decimal` 精确计算每条预测的平方误差、总体 Brier 均分，并按十个
概率区间汇总预测均值与实际为 1 的频率（可靠性分箱）。接口不修改任何数据。

**前置校验**（请求中的重复 ID 自动保序去重，按去重后顺序找到第一个不满足
条件的事件即报错）：

| HTTP | code | 场景 |
|------|------|------|
| 404 | `EVENT_NOT_FOUND` | 任一事件不存在 |
| 409 | `CALIBRATION_EVENT_NOT_SETTLED` | 任一事件尚未结算 |
| 422 | `CALIBRATION_STANDARD_RESULT_NOT_BINARY` | 任一事件结算标准结果不是逐字符精确的 `"0"` 或 `"1"`（如 `"1.0"`、`"00"`、`"true"`、`"是"` 均拒绝） |
| 422 | — | 事件 ID 数量不在 1～100、ID 非 UUID 或请求体结构错误 |

**概率原文语法**（严格匹配）：仅接受**无空白、无符号、无指数**且数值位于
闭区间 **[0, 1]** 的十进制数（仅 ASCII 数字）：

```
probability := ( 数字+ ["." 数字*] | "." 数字+ )
```

合法示例：`"0"`、`"1"`、`"0.5"`、`".25"`、`"1."`、`"00.10"`；
非法原文逐条列入响应的 `invalid` 并注明原因，**不参与计分**：

| reason | 含义 | 示例 |
|--------|------|------|
| `INVALID_FORMAT` | 含空白、正负号、指数记法，或不是十进制数 | `" 0.5"`、`"+0.5"`、`"-0"`、`"1e-1"`、`"5E0"`、`""`、`"NaN"`、`"Infinity"`、`"1_000"` |
| `OUT_OF_RANGE` | 语法为十进制数但数值不在 [0, 1] | `"1.0000000001"`、`"9"`、`"10"` |

从未揭示的承诺不参与解析与计分，单独计入 `unrevealed_commitment_count`。

**计分与分箱**（全程十进制精确计算，不经过浮点）：

- 每条有效预测的实际结果 `y` 取所属事件的结算标准结果（`"0"→0`、`"1"→1`），
  平方误差 `squared_error = (p - y)^2`；
- 总体 **Brier 均分** = 全部有效预测平方误差的算术平均；无有效预测时为
  `null`；
- 十个分箱固定返回，边界归属确定——`k/10`（k=1..9）归入**右侧**箱，
  端点 `1` 在最后一箱：

  | index | range | index | range |
  |---|---|---|---|
  | 0 | `[0.0,0.1)` | 5 | `[0.5,0.6)` |
  | 1 | `[0.1,0.2)` | 6 | `[0.6,0.7)` |
  | 2 | `[0.2,0.3)` | 7 | `[0.7,0.8)` |
  | 3 | `[0.3,0.4)` | 8 | `[0.8,0.9)` |
  | 4 | `[0.4,0.5)` | 9 | `[0.9,1.0]` |

  每箱给出 `count`（样本数）、`mean`（箱内预测均值）与 `positive_rate`
  （实际为 1 的频率）；**空样本的 `mean` 与 `positive_rate` 为 `null`**。
- 所有计算得到的数值统一**四舍六入五成双**（`ROUND_HALF_EVEN`）量化到
  **6 位小数**输出（如 `0.062500`、`0.175000`、`0.666667`）；揭示原文
  仍逐字回显在 `original_text`。
- 单条平方误差、跨事件均分与各分箱均值在最终量化前于**按输入位数自适应
  的高精度 Decimal 上下文**中计算：概率原文即使带数十位小数（如
  `0.50000049999975000024999968750043749934375103124833`），其平方误差
  也不会被默认 Decimal 上下文的 28 位有效数字提前舍入而越过五成双分界
  （该例结果为 `0.250001`）；结果与精确十进制计算一致，与请求顺序无关。
- 有效预测与无效条目均按 `(event_id, committed_at, commitment_id)` 稳定
  排序；顶层字段与 10 个分箱按固定顺序返回；`event_ids` 回显去重后的事件
  ID（按请求中首次出现的顺序）。

```bash
curl -s -X POST http://localhost:8000/events/probability-calibration-report \
  -H 'Content-Type: application/json' \
  -d '{"event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>", "<EVENT_ID_1>"]}'
```

```json
{
  "generated_at": "2030-08-01T03:00:00Z",
  "event_count": 2,
  "event_ids": ["<EVENT_ID_1>", "<EVENT_ID_2>"],
  "valid_prediction_count": 5,
  "invalid_count": 2,
  "unrevealed_commitment_count": 3,
  "brier_mean": "0.175000",
  "predictions": [
    {"event_id": "<EVENT_ID_1>", "commitment_id": "<CID_1>",
     "committed_at": "2030-08-01T00:05:00Z",
     "original_text": "1", "outcome": 1, "squared_error": "0.000000"},
    {"event_id": "<EVENT_ID_1>", "commitment_id": "<CID_2>",
     "committed_at": "2030-08-01T00:06:00Z",
     "original_text": "0.75", "outcome": 1, "squared_error": "0.062500"},
    {"event_id": "<EVENT_ID_1>", "commitment_id": "<CID_3>",
     "committed_at": "2030-08-01T00:07:00Z",
     "original_text": "0.25", "outcome": 1, "squared_error": "0.562500"},
    {"event_id": "<EVENT_ID_2>", "commitment_id": "<CID_4>",
     "committed_at": "2030-08-01T00:08:00Z",
     "original_text": "0", "outcome": 0, "squared_error": "0.000000"},
    {"event_id": "<EVENT_ID_2>", "commitment_id": "<CID_5>",
     "committed_at": "2030-08-01T00:09:00Z",
     "original_text": "0.5", "outcome": 0, "squared_error": "0.250000"}
  ],
  "invalid": [
    {"event_id": "<EVENT_ID_1>", "commitment_id": "<CID_6>",
     "committed_at": "2030-08-01T00:10:00Z",
     "original_text": "nope", "reason": "INVALID_FORMAT"},
    {"event_id": "<EVENT_ID_1>", "commitment_id": "<CID_7>",
     "committed_at": "2030-08-01T00:11:00Z",
     "original_text": "1.5", "reason": "OUT_OF_RANGE"}
  ],
  "bins": [
    {"index": 0, "range": "[0.0,0.1)", "lower_bound": "0.0", "upper_bound": "0.1",
     "count": 1, "mean": "0.000000", "positive_rate": "0.000000"},
    {"index": 1, "range": "[0.1,0.2)", "lower_bound": "0.1", "upper_bound": "0.2",
     "count": 0, "mean": null, "positive_rate": null},
    {"index": 2, "range": "[0.2,0.3)", "lower_bound": "0.2", "upper_bound": "0.3",
     "count": 1, "mean": "0.250000", "positive_rate": "1.000000"}
  ]
}
```

> 上例 `bins` 仅展示前 3 箱；实际响应始终包含全部 10 箱（`index` 0～9）。

## 错误响应

错误统一返回 `{"detail": {"code": "...", "message": "..."}}`，且**均不改变任何状态**
（所有校验先于写入，并发冲突由数据库唯一约束兜底并回滚）：

| HTTP | code | 场景 |
|------|------|------|
| 400 | `INVALID_DEADLINES` | 截止时间顺序非法或 commit_deadline 不在未来 |
| 400 | `INVALID_TRACK_LIMIT` | 资源排程的 track_limit 缺失、非整数（含布尔值）或小于 1 |
| 400 | `MISSING_SNAPSHOT_TIME` / `INVALID_SNAPSHOT_TIME` / `NAIVE_SNAPSHOT_TIME` | 历史快照缺少 `at`、时间无法解析或未携带时区 |
| 400 | `SNAPSHOT_BEFORE_CREATION` | 快照时刻早于事件创建时间 |
| 404 | `EVENT_NOT_FOUND` | 事件不存在（含冲突分析/资源排程/复用审计请求中的任一 ID） |
| 404 | `COMMITMENT_NOT_FOUND` | 集合成员证明的承诺不存在或不属于该事件 |
| 409 | `EXTENSION_NOT_IN_COMMIT_PHASE` | 延长截止时间时事件已离开 commit 阶段（reveal / closed） |
| 409 | `INVALID_EXTENSION` | 延长请求未满足「只延不缩」或 reveal 须晚于新 commit 的约束 |
| 409 | `COMMIT_PHASE_CLOSED` | 迟到提交（已过 commit_deadline） |
| 409 | `DUPLICATE_COMMITMENT` | 同一事件内重复的承诺哈希（单个提交端点） |
| 409 | `BATCH_COMMITMENT_CONFLICT` | 批量提交存在逐项冲突（`detail.conflicts` 给出每个冲突项的 index/type/hash） |
| 409 | `REVEAL_PHASE_NOT_STARTED` | 提前揭示（未到 commit_deadline） |
| 409 | `REVEAL_PHASE_CLOSED` | 逾期揭示（已过 reveal_deadline） |
| 409 | `NO_MATCHING_COMMITMENT` | 原文 + 盐重算后不匹配任何承诺 |
| 409 | `DUPLICATE_REVEAL` | 该承诺已被揭示（仅首次匹配生效） |
| 409 | `BATCH_REVEAL_CONFLICT` | 批量揭示存在逐项冲突（`detail.conflicts` 给出每个冲突项的 index/type 与原文、盐、重算哈希） |
| 409 | `SETTLEMENT_NOT_IN_CLOSED_PHASE` | 事件尚未进入 closed 阶段即提交结算 |
| 409 | `PROOF_NOT_AVAILABLE` | 未过提交截止即请求承诺集合 Merkle 根或成员证明 |
| 409 | `SETTLEMENT_ALREADY_CONFIRMED` | 事件已永久结算，重复结算被拒绝且不改写既有结果（`detail.conflicts[0].existing` 回显首次结果） |
| 409 | `INTERVAL_REPORT_NOT_SETTLED` | 数值区间报告仅对已结算事件生成，事件尚未结算 |
| 409 | `CALIBRATION_EVENT_NOT_SETTLED` | 跨事件概率校准报告中的任一事件尚未结算 |
| 422 | `STANDARD_RESULT_NOT_FINITE_DECIMAL` | 结算标准结果不是有限十进制数，无法生成数值区间报告 |
| 422 | `INTERVAL_WIDTH_TOO_LARGE` | 数值区间报告中存在精确宽度有效数字位数超过 4096 位的合法区间，拒绝舍入 |
| 422 | `CALIBRATION_STANDARD_RESULT_NOT_BINARY` | 概率校准事件的结算标准结果不是精确的 "0" 或 "1" |
| 422 | — | 请求体格式校验失败（如哈希非 64 位十六进制、截止时间无时区、延长缺少 reason、结算标准结果为空、概率校准事件 ID 数量不在 1～100） |
| 503 | `SCHEMA_NOT_READY` | 启动期结构核对发现持久库缺表或缺列（半旧库）：`/health` 与全部业务接口均返回 503，响应中列出缺失表名与列名；服务不补建、不迁移，需人工修正库结构后重启 |

「仅首次匹配的揭示生效」由 `reveals.commitment_id` 唯一约束保证：并发揭示同一承诺时，
后到者事务回滚并收到 `DUPLICATE_REVEAL`。

## 本地开发与测试

### Docker Compose 启动（推荐）

前置条件：已安装 Docker 与 Docker Compose Plugin（`docker compose version` 可用）。

```bash
docker compose up --build       # 前台启动（加 -d 后台运行）
docker compose ps               # 查看两个容器健康状态（db / api 均为 healthy 即就绪）
docker compose logs -f api      # 跟踪 API 日志（含空库初始化 / 半旧库拒绝就绪的结构核对日志）
docker compose down             # 停止并删除容器（保留数据卷）
docker compose down -v          # 停止并连同 Postgres 数据卷 pgdata 一起删除（彻底重置）
```

Compose 编排说明（见 `docker-compose.yml`）：

- **db**：PostgreSQL 16（`postgres:16-alpine`），库名/用户名/密码均为
  `postgres` / `postgres` / `predictions`，数据持久化在命名卷 `pgdata` 中，
  宿主端口 `5432`；自带 `pg_isready` healthcheck。
- **api**：由仓库 `Dockerfile` 构建（`python:3.12-slim` + `requirements.txt`，
  启动命令为 `uvicorn app.main:app --host 0.0.0.0 --port 8000`），
  通过 `depends_on: condition: service_healthy` 等待数据库健康后再启动，
  启动时核对/初始化库结构（空库建表；已有库逐表核对全部表与必需列，半旧库
  拒绝就绪，详见上文「启动期数据库结构核对」；涉及表 `events` / `commitments` /
  `reveals` / `deadline_extensions` / `settlements`），宿主端口 `8000`；
  自带 `/health` healthcheck。
- API 通过环境变量 `DATABASE_URL=postgresql+psycopg2://postgres:postgres@db:5432/predictions`
  连接数据库；如本机 5432 或 8000 端口被占用，可在 `docker-compose.yml`
  中修改 `ports` 映射（容器内端口不变）。

启动后即可访问：

- API 地址：**http://localhost:8000**
- 交互式文档（Swagger UI）：**http://localhost:8000/docs**
- 健康检查：`curl http://localhost:8000/health`
  - 结构核对通过 → `200`：`{"status":"ok"}`
  - 持久库缺表/缺列（半旧库）→ `503`：`{"status":"not_ready",
    "code":"SCHEMA_NOT_READY", "message":"…", "missing_tables":[…],
    "missing_columns":{"<表名>": ["<缺失列>", …]}}`（此时业务接口同样返回 503，
    容器在 `docker compose ps` 中显示 `unhealthy`）

### 不使用 Docker（本地直跑）

```bash
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./dev.db uvicorn app.main:app --reload   # 本地运行（默认连 PostgreSQL）
python3 -m pytest tests/ -q                                     # 运行测试（SQLite + 模拟时钟）
```

测试使用临时 SQLite 文件库并猴子补丁服务端时钟，无需启动 Postgres；
生产/Compose 环境的行锁（`SELECT … FOR UPDATE`）在 SQLite 上自动降级为普通
查询，并发正确性仍由唯一约束保证。

## 项目结构

```
app/
  main.py       # 路由、生命周期、结构就绪门控、时间判定与错误处理
  clock.py      # 统一时钟（阶段判定与记录 created_at 的唯一时间来源，可整体替换）
  conflicts.py  # 阶段窗口 sweep-line 冲突分析（纯函数，可独立单测）
  scheduling.py # 阶段窗口资源排程：最少轨道的确定性区间着色（纯函数，可独立单测）
  models.py     # SQLAlchemy 模型（events / commitments / reveals / deadline_extensions / settlements）
  schema_bootstrap.py # 启动期库结构核对：空库建表；已有库逐表核对必需表/列，缺项只报告不补建
  schemas.py    # Pydantic 请求/响应模型
  hashing.py    # 承诺哈希方案实现
  integrity_audit.py # 只读完整性巡检命令（python -m app.integrity_audit）：同一一致性只读快照中核对揭示哈希与结算固化计数
  merkle.py     # 承诺集合 Merkle 树：叶/内部域前缀、末项复制、成员证明与严格校验（纯函数）
  intervals.py  # 数值区间预测报告：有限十进制解析、精确覆盖判定与命中排名（纯函数）
  calibration.py # 跨事件概率校准报告：[0,1] 概率严格解析、Decimal Brier 均分与十分箱汇总（纯函数）
  database.py   # 引擎与会话（DATABASE_URL 可配置）
tests/test_api.py             # 业务规则冒烟测试（SQLite + 模拟时钟）
tests/test_request_timestamp.py # 准入判定与 created_at 共用同一请求时刻：跨截止点不串阶段、批量共用时刻、拒绝不落库
tests/test_schema_bootstrap.py # 启动期结构核对：空库初始化、半旧库拒绝就绪（503 列缺失表/列）、完整旧库保留数据
tests/test_snapshot.py        # 历史时刻快照：延长回放、阶段与计数重放、400/404 错误
tests/test_conflicts.py       # 冲突分析：sweep-line 边界、排序与 API 用例
tests/test_schedule.py        # 资源排程：最少轨道着色、半开复用、冲突对与确定性
tests/test_reuse_audit.py     # 跨事件承诺复用审计：归一化查重、严格早于揭示判定、排序与只读
tests/test_extend.py          # 截止时间延长：只延不缩、阶段限制、幂等与时间线
tests/test_extend_same_timestamp.py # 同刻连续延长：按延长链新截止时间排序的快照、幂等重放与时间线回归
tests/test_batch_commitments.py  # 批量承诺：逐项冲突、整体回滚与并发兜底
tests/test_batch_reveals.py   # 批量揭示：四类逐项冲突、整体回滚与并发仅首次生效
tests/test_reveal_settlement_concurrency.py # 揭示（单条/批量）与结算截止边界并发：事件行锁串行、先提交必计入、结算先完成则后到揭示拒绝不落库
tests/test_settlement.py      # 永久结算：精确匹配计数、未结算状态、重复结算冲突与不改写
tests/test_interval_report.py # 数值区间报告：十进制精确覆盖判定、命中排名、无效/未揭示分类与只读
tests/test_calibration_report.py # 跨事件概率校准：严格概率解析、Brier 均分、十分箱边界与五成双量化
tests/test_merkle.py          # Merkle 纯函数：空根/单叶、末项复制、域分离、成员证明与严格层级校验
tests/test_merkle_proofs_api.py  # 集合证明 API：阶段门禁、根/证明签发、本地校验、404/409 与现有流程不受影响
tests/test_integrity_audit.py # 只读完整性巡检：揭示/结算核对、稳定问题代码、退出码、只读保证与子进程端到端
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
