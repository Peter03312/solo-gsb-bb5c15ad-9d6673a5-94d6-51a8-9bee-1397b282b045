# 计量校准证书追溯服务

可离线运行的计量校准证书签发与**量值溯源链**管理服务。以基准（标准）设备的合格证书为
追溯起点，普通校准证书必须引用一张在**校准时刻有效且合格**的标准设备证书；撤销上游
证书时，其全部下游证书级联失效。

自本版起支持**交接批次（issue batch）**：实验室离线采集的一组校准记录可携带唯一批次键
整批签发——全部记录复用现有基准/普通证书字段与校验规则，批内普通证书可引用库内或本批
证书；任一记录不合规则整批不落库；同键重试幂等返回首次签发结果。

- 技术栈：Python 3.11 · FastAPI · PostgreSQL 16 · psycopg3
- 关键业务不变量由**数据库约束 + 触发器 + 单事务**保证，不依赖应用层“先查后写”，
  因此在并发下依然成立。

---

## 目录结构

```
.
├── app/                 # FastAPI 应用
│   ├── main.py          # 入口、异常处理、健康检查
│   ├── routers.py       # HTTP 路由
│   ├── repository.py    # 核心业务规则（签发校验/成环检测/级联失效）
│   ├── schemas.py       # 请求/响应模型
│   ├── db.py            # 连接池
│   ├── config.py        # 环境变量配置
│   └── errors.py        # 统一业务错误
├── db/init.sql          # 数据库初始化（枚举、约束、触发器、视图）
├── db/migrations/       # 已部署旧库的增量迁移脚本（幂等）
├── scripts/             # 冒烟测试脚本（可直接对运行中的服务执行）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

---

## 一键启动（Docker Compose）

```bash
docker compose up -d --build
```

首次启动时：

1. 启动 PostgreSQL 16，并在**空数据卷**上自动执行 `db/init.sql` 完成建表；
2. 等待数据库健康检查通过后启动 API。

服务地址与端口：

| 服务 | 地址 |
| --- | --- |
| API 基址 | <http://localhost:8080> |
| 健康检查 | <http://localhost:8080/health> |
| 交互式文档（Swagger） | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |
| PostgreSQL（仅绑定宿主机回环地址） | `127.0.0.1:5432` |

停止与清理：

```bash
docker compose down          # 停止，保留数据卷（证书记录保留）
docker compose down -v       # 停止并删除数据卷（下次启动重新初始化空库）
```

> 完全离线部署：在有网络的机器上先 `docker compose build`（或 `docker save` 导出
> `python:3.11-slim` 与 `postgres:16-alpine` 镜像），拷贝到内网环境 `docker compose up -d`
> 即可，运行期不访问任何外部地址。

---

## 配置

所有配置通过环境变量注入，可在项目根目录放置 `.env`（参考 `.env.example`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `APP_PORT` | `8080` | API 在宿主机暴露的端口 |
| `POSTGRES_USER` | `caltrace` | 数据库用户 |
| `POSTGRES_PASSWORD` | `caltrace` | 数据库密码（生产请修改） |
| `POSTGRES_DB` | `caltrace` | 数据库名 |
| `POSTGRES_PORT` | `127.0.0.1:5432` | 数据库在宿主机的绑定地址:端口 |
| `DB_HOST` / `DB_PORT` | `db` / `5432` | API 容器内连接数据库用（compose 已固定） |

### 不用 Compose、直连已有 PostgreSQL

```bash
pip install -r requirements.txt
psql "$DB_URL" -f db/init.sql          # 全新空库；需 superuser 创建 btree_gist 扩展
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=caltrace DB_USER=caltrace DB_PASSWORD=secret \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

要求 PostgreSQL 14+，当前库内可执行 `CREATE EXTENSION btree_gist`（contrib 自带）。

**从旧版本升级**（已运行、库里有历史证书）不要重跑 `init.sql`，改用增量迁移：

```bash
psql "$DB_URL" -f db/migrations/001_issue_batches.sql   # 幂等，可重复执行
```

迁移只新增 `issue_batches` 表、`certificates.issue_batch_key` 列、相关触发器与视图字段，
不改动任何既有证书数据。

---

## 业务规则

1. **追溯起点**：基准设备证书（`kind=REFERENCE`）无上游引用，结论恒为合格。
2. **引用有效性**：普通证书（`kind=CALIBRATION`）必须引用一张标准设备证书，
   且该校准证书在本证书的**校准时刻**满足：状态生效、结论合格、校准时刻落在其
   有效期 `[valid_from, valid_to)` 内。已撤销（REVOKED）或已失效（INVALID）的
   证书不能再作为校准依据。
3. **引用链不可成环**：树状溯源结构 + 签发时防御性成环检测 + 递归 CTE `CYCLE` 保护。
4. **有效期不重叠**：同一设备处于 ACTIVE 的证书，有效期区间不得重叠
   （`EXCLUDE USING gist` 排他约束，半开区间 `[from, to)`，首尾相接允许）。
   证书被撤销/失效后区间释放，可重新签发。
5. **不可改写**：证书签发后业务字段禁止 UPDATE、禁止 DELETE（数据库触发器强制，
   直连数据库修改也会被拒绝）；状态只允许 `ACTIVE → REVOKED/INVALID`。
6. **级联失效**：撤销一张证书 → 其所有直接/间接下游 ACTIVE 证书在**同一事务**中
   置为 INVALID，受影响证书及完整引用链快照写入 `invalidations` 表，历史签发内容
   永久可查。
7. **并发安全**：
   - 相同证书编号并发签发 → 唯一约束保证仅一张成功，其余 `409 cert_duplicate`；
   - 同设备重叠区间并发签发 → 应用预检 `422 validity_overlap`，竞态穿透时由 GiST
     排他约束兜底返回 `409 validity_overlap_concurrent`；
   - 撤销与新签发并发 → 撤销事务持有被撤销证书行锁与全部下游行锁，
     新引用插入的外键检查会阻塞到撤销提交，随后按“引用已失效”被拒绝，
     不会出现“失效证书还能被引用”的窗口。

证书状态：`ACTIVE` 生效中 / `REVOKED` 被主动撤销 / `INVALID` 因上游撤销级联失效。

8. **交接批次整批签发**：`POST /api/v1/issue-batches` 携带客户端生成的唯一 `batch_key`
   与非空 `certificates` 列表，在**单事务**内完成。
   - 每项沿用基准/普通证书的全部字段与上述签发规则；`kind=REFERENCE` 不得带
     `parent_cert_no`（结论恒 PASS），`kind=CALIBRATION` 必须带 `conclusion` 与
     `parent_cert_no`；
   - 普通证书的 `parent_cert_no` **可引用库内已有证书或本批证书**；列表顺序不代表
     引用顺序，服务端按引用关系自动排序写入；
   - 批内额外校验：编号唯一（且不与库内冲突）、批内引用无环、引用的库内上游当前
     ACTIVE/PASS 且校准时刻在其有效期内、批内上游同样须合格且覆盖校准时刻、
     同设备有效期彼此不重叠且不与库内 ACTIVE 证书重叠；
   - **任一记录不合规 → 整批回滚不落库**，错误逐项给出列表下标 `index`、`cert_no`、
     `code` 与原因；
   - **幂等**：同 `batch_key` + 同请求（证书内容一致、列表顺序无关）重试，返回首次
     签发结果（`200` + 响应头 `Idempotency-Replayed: true`，体内 `replayed=true`），
     即使其中证书后来被撤销也原样返回首次快照；同 `batch_key` + 不同请求 →
     `409 issue_batch_conflict`；校验失败的批次键随回滚释放，可修正后重试；
   - 批次并发安全：批次键行的事务锁串行化同键提交；与单张签发/撤销并发时，由唯一
     约束、GiST 排他约束、外键行锁与单事务共同保证原子性，穿透的竞态统一返回
     `409 issue_batch_concurrent`（整批未签发，可安全重试）。

---

## HTTP API

所有时间字段为带时区 ISO 8601，例如 `2026-03-01T09:00:00+08:00`。

### 1. 登记基准设备合格证书（追溯起点）

```bash
curl -sS -X POST http://localhost:8080/api/v1/reference-certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "REF-MASTER-001",
    "device_id": "DEV-MASTER",
    "device_name": "实验室最高标准器",
    "valid_from": "2026-01-01T00:00:00+08:00",
    "valid_to":   "2027-01-01T00:00:00+08:00",
    "calibrated_at": "2026-01-01T09:00:00+08:00",
    "operator": "张工",
    "remark": "年度基准登记"
  }'
```

`calibrated_at` 可省略，缺省取 `valid_from`。响应 `201`，含证书与其追溯链。

### 2. 签发普通校准证书

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-WS-001",
    "device_id": "DEV-WS-A",
    "device_name": "车间万用表A",
    "valid_from": "2026-03-01T00:00:00+08:00",
    "valid_to":   "2027-03-01T00:00:00+08:00",
    "calibrated_at": "2026-03-05T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "REF-MASTER-001",
    "operator": "李工"
  }'
```

再向下签发一层（引用普通证书）：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "cert_no": "CAL-CUS-001",
    "device_id": "DEV-CUS-B",
    "device_name": "客户仪表B",
    "valid_from": "2026-04-01T00:00:00+08:00",
    "valid_to":   "2026-10-01T00:00:00+08:00",
    "calibrated_at": "2026-04-02T10:00:00+08:00",
    "conclusion": "PASS",
    "parent_cert_no": "CAL-WS-001"
  }'
```

### 2b. 交接批次整批签发（离线校准记录一次交接）

一组记录随**唯一批次键**整批提交；列表顺序随意，批内可互相引用，也可引用库内证书。

```bash
curl -sS -X POST http://localhost:8080/api/v1/issue-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOVER-2026-0315-01",
    "certificates": [
      {
        "cert_no": "B-CAL-0002",
        "kind": "CALIBRATION",
        "device_id": "DEV-WS-A",
        "device_name": "车间万用表A",
        "valid_from": "2026-03-10T00:00:00+08:00",
        "valid_to": "2027-03-10T00:00:00+08:00",
        "calibrated_at": "2026-03-15T10:30:00+08:00",
        "conclusion": "PASS",
        "parent_cert_no": "B-CAL-0001"
      },
      {
        "cert_no": "B-REF-0001",
        "kind": "REFERENCE",
        "device_id": "DEV-MASTER-2",
        "device_name": "二号实验室标准器",
        "valid_from": "2026-01-01T00:00:00+08:00",
        "valid_to": "2027-01-01T00:00:00+08:00",
        "calibrated_at": "2026-01-01T09:00:00+08:00",
        "operator": "张工"
      },
      {
        "cert_no": "B-CAL-0001",
        "kind": "CALIBRATION",
        "device_id": "DEV-WS-Z",
        "device_name": "车间标准源Z",
        "valid_from": "2026-03-01T00:00:00+08:00",
        "valid_to": "2027-03-01T00:00:00+08:00",
        "calibrated_at": "2026-03-12T09:00:00+08:00",
        "conclusion": "PASS",
        "parent_cert_no": "REF-MASTER-001"
      }
    ]
  }'
```

字段规则与单张接口完全一致（每项仍是基准或普通证书的字段集）；区别仅在于每项多一个
`kind`，且普通证书的 `parent_cert_no` 允许指向本批 `cert_no`。

成功 `201`：

```json
{
  "batch_key": "HANDOVER-2026-0315-01",
  "issued_at": "2026-03-15T03:00:00+00:00",
  "replayed": false,
  "cert_count": 3,
  "certificates": [
    {"cert_no": "B-CAL-0002", "certificate": {"...": "完整证书"},
     "chain": {"root_cert_no": "REF-MASTER-001", "length": 3, "nodes": [ {"...": "基准到自身的链"} ]}},
    {"cert_no": "B-REF-0001", "certificate": {"...": "..."}, "chain": {"...": "..."}},
    {"cert_no": "B-CAL-0001", "certificate": {"...": "..."}, "chain": {"...": "..."}}
  ]
}
```

`certificates` 与请求列表**同序**返回，每项都带从基准到自身的追溯链。

任一记录不合规则整批拒绝，`422 batch_validation_failed`，`details.errors[]` 逐项定位：

```json
{
  "error": {
    "code": "batch_validation_failed",
    "message": "批次 ... 中有 2 项记录不合规，整批不予签发（任何记录未入库）",
    "details": {
      "batch_key": "HANDOVER-2026-0315-01",
      "errors": [
        {"index": 1, "cert_no": "B-CAL-0002", "code": "reference_cycle",
         "message": "批内引用关系将形成环路（证书 B-CAL-0002 位于环上）"},
        {"index": 2, "cert_no": "B-CAL-0007", "code": "validity_overlap",
         "message": "设备 DEV-WS-A 已生效证书 ... 的有效期与本批证书 ... 重叠",
         "details": {"conflict_cert_no": "CAL-WS-001"}}
      ]
    }
  }
}
```

幂等行为：

```bash
# 同键同请求重试（甚至列表顺序不同）→ 200 + 响应头 Idempotency-Replayed: true
curl -sS -X POST http://localhost:8080/api/v1/issue-batches \
  -H 'Content-Type: application/json' \
  -d '{ "batch_key": "HANDOVER-2026-0315-01", "certificates": [ ...同内容... ] }'

# 同键但请求内容不同 → 409 issue_batch_conflict（details.first_request 回带首次请求指纹）
```

批次查询：

```bash
# 批次列表
curl -sS 'http://localhost:8080/api/v1/issue-batches?limit=50&offset=0'

# 批次详情：元信息 + 各证书的【当前】状态与追溯链（证书若被撤销/失效在此反映；
# 而 POST 同键重放永远返回首次签发快照）
curl -sS http://localhost:8080/api/v1/issue-batches/HANDOVER-2026-0315-01
```


### 3. 查询追溯引用链

```bash
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001/chain | python3 -m json.tool
```

返回从基准到目标证书的有序节点链（`nodes[0]` 即追溯起点）。

### 4. 查询某证书的全部下游（撤销前影响面预览）

```bash
curl -sS http://localhost:8080/api/v1/certificates/REF-MASTER-001/downstream
```

### 5. 撤销证书（级联失效下游，返回受影响证书与引用链）

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificates/REF-MASTER-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"reason": "年度比对发现超差", "operator": "质量负责人"}'
```

响应示例（节选）：

```json
{
  "batch_id": 1,
  "revoked": {"cert_no": "REF-MASTER-001", "status": "REVOKED", ...},
  "affected_count": 2,
  "affected": [
    {"cert_no": "CAL-WS-001",  "depth": 1, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}]},
    {"cert_no": "CAL-CUS-001", "depth": 2, "chain": [{"cert_no": "REF-MASTER-001", ...}, {"cert_no": "CAL-WS-001", ...}, {"cert_no": "CAL-CUS-001", ...}]}
  ]
}
```

### 6. 查询证书 / 列表 / 设备历史 / 撤销批次

```bash
# 单张证书（无论 ACTIVE / REVOKED / INVALID，历史内容始终可查）
curl -sS http://localhost:8080/api/v1/certificates/CAL-CUS-001

# 列表，支持 device_id / status / kind 过滤与分页
curl -sS 'http://localhost:8080/api/v1/certificates?status=INVALID&limit=50&offset=0'

# 某设备的全部证书（含已撤销/已失效的历史记录）
curl -sS http://localhost:8080/api/v1/devices/DEV-CUS-B/certificates

# 撤销批次列表 / 详情
curl -sS http://localhost:8080/api/v1/revocations
curl -sS http://localhost:8080/api/v1/revocations/1
```

---

## 错误响应

统一格式：`{"error": {"code": "...", "message": "...", "details": {...}}}`

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cert_not_found` / `device_not_found` / `revocation_not_found` | 目标不存在 |
| 409 | `cert_duplicate` | 证书编号已存在（含并发重复签发） |
| 422 | `invalid_reference` | 引用的标准证书不存在 |
| 422 | `reference_not_active` | 引用了已撤销/已失效证书（失效证书不得作依据） |
| 422 | `reference_not_qualified` | 引用的标准证书结论为不合格 |
| 422 | `reference_out_of_validity` | 校准时刻不在标准证书有效期内 |
| 422 | `reference_cycle` | 引用关系将形成环路 |
| 422 | `validity_overlap` | 同设备已生效证书有效期重叠 |
| 409 | `validity_overlap_concurrent` | 并发签发重叠，排他约束兜底拒绝 |
| 409 | `cert_not_active` | 撤销一张非 ACTIVE 证书 |
| 422 | `batch_validation_failed` | 交接批次中存在不合规记录，整批不落库；`details.errors[]` 逐项给出 `index`/`cert_no`/`code`/`message`，单项 `code` 复用上述各校验码，另含 `batch_cert_no_duplicate`（批内编号重复）、`reference_with_parent`（基准证书带了上游）、`conclusion_required`（普通证书缺结论） |
| 409 | `issue_batch_conflict` | 同 `batch_key` 已由另一个不同请求成功签发 |
| 409 | `issue_batch_concurrent` | 批次与并发单张签发/撤销冲突，整批未签发，可重试 |
| 404 | `issue_batch_not_found` | 交接批次不存在 |
| 409 | `issue_batch_incomplete` | 批次处于未完成状态（正常不会出现） |

> 交接批次的单项错误码与单张签发完全一致（`invalid_reference` /
> `reference_not_active` / `reference_not_qualified` /
> `reference_out_of_validity` / `reference_cycle` / `validity_overlap` /
> `cert_duplicate`），只是统一包在 `batch_validation_failed` 的 `errors[]` 中返回。

---

## 自测

服务启动后执行（仅依赖 bash + curl / Python 标准库）：

```bash
scripts/smoke-test.sh                 # 17 项主流程与错误路径
python3 scripts/extra_checks.py       # 21 项并发/边界/中段撤销补充检查
python3 scripts/batch_checks.py       # 79 项交接批次：整批/幂等/冲突/成环/重叠/并发
```

覆盖：基准登记、多级签发、无效引用、越界校准时刻、区间重叠（含首尾相接）、
同编号并发与重叠区间并发（3 线程仅 1 成功）、中段撤销、级联失效、失效证书
禁止引用、撤销后重签、历史可查、撤销批次回放，以及数据库层 UPDATE/DELETE 封印；
交接批次专项另覆盖批内基准/多级引用（乱序）、引用库内证书、同键重试返回首次快照
（即使撤销后）、同键异请求冲突、失败后可重试、整批失败不落库、成环/重叠双方编号
定位、6 线程同键仅 1 次 201、批次与单张/撤销并发的原子性。
