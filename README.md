# 计量校准证书追溯服务

可离线运行的计量校准证书签发与**量值溯源链**管理服务。以基准（标准）设备的合格证书为
追溯起点，普通校准证书必须引用一张在**校准时刻有效且合格**的标准设备证书；撤销上游
证书时，其全部下游证书级联失效。离线采集的校准记录可作为**交接批次**整批签发：
批次键幂等、整批原子落库、批内证书可相互引用。

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
├── db/init.sql          # 数据库初始化（枚举、约束、触发器、视图、签发批次表）
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
psql "$DB_URL" -f db/init.sql          # 需 superuser 创建 btree_gist 扩展
DB_HOST=127.0.0.1 DB_PORT=5432 DB_NAME=caltrace DB_USER=caltrace DB_PASSWORD=secret \
  uvicorn app.main:app --host 0.0.0.0 --port 8080
```

要求 PostgreSQL 14+，当前库内可执行 `CREATE EXTENSION btree_gist`（contrib 自带）。

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
8. **批次签发（离线交接）**：`POST /api/v1/certificate-batches` 携带唯一批次键与
   非空证书列表，每项沿用单张签发的全部字段与规则；普通证书可引用库内证书或
   本批证书（**列表顺序不代表引用顺序**，服务按引用拓扑排序后逐张校验落库）。
   批内编号唯一、引用无环、上游当前生效且合格且校准时刻落在其有效期内、同设备
   生效有效期不重叠——**任一记录不合规则整批不落库**，错误响应以
   `details.cert_no` 定位出错编号。批次键为幂等键：同键同请求重放首次签发结果
   快照（即使证书事后被撤销），同键不同请求返回 `409 batch_conflict`；
   并发重复提交由批次键主键兜底，仅一个事务真正落库。

证书状态：`ACTIVE` 生效中 / `REVOKED` 被主动撤销 / `INVALID` 因上游撤销级联失效。

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

### 7. 批量签发交接批次（整批原子、批次键幂等）

离线采集的一组校准记录作为一个交接批次一次签发。每项通过 `kind` 区分基准
（`REFERENCE`）或普通（`CALIBRATION`）证书，字段与单张签发一致；普通证书的
`parent_cert_no` 可引用库内证书或本批证书，**列表顺序不代表引用顺序**：

```bash
curl -sS -X POST http://localhost:8080/api/v1/certificate-batches \
  -H 'Content-Type: application/json' \
  -d '{
    "batch_key": "HANDOFF-2026-10-03-001",
    "certificates": [
      {"kind": "CALIBRATION", "cert_no": "CAL-B-002", "device_id": "DEV-B-2",
       "valid_from": "2026-05-01T00:00:00+08:00", "valid_to": "2027-05-01T00:00:00+08:00",
       "calibrated_at": "2026-05-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "CAL-B-001"},
      {"kind": "REFERENCE", "cert_no": "REF-B-000", "device_id": "DEV-B-0",
       "valid_from": "2026-01-01T00:00:00+08:00", "valid_to": "2027-01-01T00:00:00+08:00",
       "calibrated_at": "2026-01-01T09:00:00+08:00"},
      {"kind": "CALIBRATION", "cert_no": "CAL-B-001", "device_id": "DEV-B-1",
       "valid_from": "2026-03-01T00:00:00+08:00", "valid_to": "2027-03-01T00:00:00+08:00",
       "calibrated_at": "2026-03-02T09:00:00+08:00", "conclusion": "PASS",
       "parent_cert_no": "REF-B-000"}
    ]
  }'
```

首次成功响应 `201`（`items` 按请求列表顺序返回，各含从基准到自身的追溯链）：

```json
{
  "batch_key": "HANDOFF-2026-10-03-001",
  "count": 3,
  "issued_at": "2026-10-03T10:20:30.123456+00:00",
  "items": [
    {"certificate": {"cert_no": "CAL-B-002", ...},
     "chain": {"cert_no": "CAL-B-002", "root_cert_no": "REF-B-000", "length": 3, "nodes": [...]}},
    {"certificate": {"cert_no": "REF-B-000", ...}, "chain": {...}},
    {"certificate": {"cert_no": "CAL-B-001", ...}, "chain": {...}}
  ]
}
```

幂等与冲突约定：

- **同键同请求重试** → `200`，返回首次签发结果快照（即使证书事后被撤销）；
- **同键不同请求** → `409 batch_conflict`；
- **任一记录不合规** → 整批不落库，错误响应 `details.cert_no` 定位出错编号，
  错误码与单张签发一致（如 `reference_not_active`、`validity_overlap`），
  批内特有问题使用 `batch_cert_duplicate`（批内编号重复）与
  `reference_cycle`（批内引用成环）。

### 8. 查询签发批次（首次签发结果快照）

```bash
curl -sS http://localhost:8080/api/v1/certificate-batches/HANDOFF-2026-10-03-001 | python3 -m json.tool
```

返回与签发响应同构的快照，不受事后撤销影响；批次不存在返回 `404 batch_not_found`。

---

## 错误响应

统一格式：`{"error": {"code": "...", "message": "...", "details": {...}}}`

| HTTP | code | 触发场景 |
| --- | --- | --- |
| 404 | `cert_not_found` / `device_not_found` / `revocation_not_found` / `batch_not_found` | 目标不存在 |
| 409 | `cert_duplicate` | 证书编号已存在（含并发重复签发、批次与库内冲突） |
| 409 | `batch_conflict` | 同一批次键提交了内容不同的签发请求 |
| 422 | `batch_cert_duplicate` | 批次内证书编号重复 |
| 422 | `invalid_reference` | 引用的标准证书不存在（库内与本批均找不到） |
| 422 | `reference_not_active` | 引用了已撤销/已失效证书（失效证书不得作依据） |
| 422 | `reference_not_qualified` | 引用的标准证书结论为不合格 |
| 422 | `reference_out_of_validity` | 校准时刻不在标准证书有效期内 |
| 422 | `reference_cycle` | 引用关系将形成环路（含批内成环） |
| 422 | `validity_overlap` | 同设备已生效证书有效期重叠（含批内重叠） |
| 409 | `validity_overlap_concurrent` | 并发签发重叠，排他约束兜底拒绝 |
| 409 | `cert_not_active` | 撤销一张非 ACTIVE 证书 |

> 批次签发中由单条记录触发的校验错误与单张签发同码，并在 `details.cert_no`
> 中给出出错证书编号（必要时附 `details.parent_cert_no` / `details.cycle` 等）。

---

## 自测

服务启动后执行（仅依赖 bash + curl / python3 标准库）：

```bash
scripts/smoke-test.sh                 # 17 项主流程与错误路径
python3 scripts/extra_checks.py       # 21 项并发/边界/中段撤销补充检查
python3 scripts/batch_checks.py       # 批次签发：原子性/乱序引用/幂等重放/键冲突/并发
```

覆盖：基准登记、多级签发、无效引用、越界校准时刻、区间重叠（含首尾相接）、
同编号并发与重叠区间并发（3 线程仅 1 成功）、中段撤销、级联失效、失效证书
禁止引用、撤销后重签、历史可查、撤销批次回放，数据库层 UPDATE/DELETE 封印，
以及批次签发的整批原子性、批内乱序引用与拓扑校验、同键重放（含事后撤销）、
同键冲突、并发重复提交（5 线程仅 1 次首次签发）与批次查询。
