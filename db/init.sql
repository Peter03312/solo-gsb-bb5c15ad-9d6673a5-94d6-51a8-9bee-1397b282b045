-- ============================================================================
-- 计量校准证书追溯服务 - 数据库结构初始化 (PostgreSQL 16)
-- 幂等说明：本脚本面向全新数据卷执行（docker-entrypoint-initdb.d 仅在空库时运行）。
-- ============================================================================

-- btree_gist 提供排他约束中对普通标量列使用 = 操作符的 GiST 支持
CREATE EXTENSION IF NOT EXISTS btree_gist;

-- ----------------------------------------------------------------------------
-- 枚举类型
-- ----------------------------------------------------------------------------

-- 证书类型：REFERENCE = 基准/标准设备证书（追溯起点，无上游引用）
--           CALIBRATION = 普通校准证书（必须引用一张有效的标准设备证书）
CREATE TYPE cert_kind AS ENUM ('REFERENCE', 'CALIBRATION');

-- 证书状态：ACTIVE   = 生效中
--           REVOKED  = 被主动撤销（仅上游证书可被主动撤销）
--           INVALID  = 因上游撤销而级联失效
CREATE TYPE cert_status AS ENUM ('ACTIVE', 'REVOKED', 'INVALID');

-- 校准结论：PASS 合格 / FAIL 不合格
CREATE TYPE cert_conclusion AS ENUM ('PASS', 'FAIL');

-- ----------------------------------------------------------------------------
-- 证书主表
-- ----------------------------------------------------------------------------
CREATE TABLE certificates (
    cert_no        TEXT PRIMARY KEY,                 -- 证书编号（业务唯一键）
    kind           cert_kind     NOT NULL,
    device_id      TEXT          NOT NULL,           -- 被校/基准设备标识
    device_name    TEXT,                             -- 设备名称（冗余展示用）
    calibrated_at  TIMESTAMPTZ   NOT NULL,           -- 校准时刻
    valid_from     TIMESTAMPTZ   NOT NULL,           -- 有效期开始
    valid_to       TIMESTAMPTZ   NOT NULL,           -- 有效期结束
    conclusion     cert_conclusion NOT NULL,         -- 校准结论
    parent_cert_no TEXT REFERENCES certificates(cert_no), -- 引用的上游标准设备证书
    operator       TEXT,                             -- 签发/校准人员
    remark         TEXT,                             -- 备注
    status         cert_status   NOT NULL DEFAULT 'ACTIVE',
    revoked_reason TEXT,                             -- 撤销原因（撤销时写入）
    revoked_at     TIMESTAMPTZ,                      -- 撤销/级联失效时刻
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- 基本完整性约束
ALTER TABLE certificates ADD CONSTRAINT cert_validity_order
    CHECK (valid_from < valid_to);
ALTER TABLE certificates ADD CONSTRAINT cert_calibrated_in_window
    CHECK (calibrated_at >= valid_from AND calibrated_at < valid_to);
-- 基准证书不得引用上游；普通证书必须引用上游
ALTER TABLE certificates ADD CONSTRAINT cert_reference_root
    CHECK ((kind = 'REFERENCE') = (parent_cert_no IS NULL));
-- 基准证书必须合格（它是量值溯源起点）；被主动撤销的证书必须记录原因与时间
ALTER TABLE certificates ADD CONSTRAINT cert_reference_passes
    CHECK (kind = 'CALIBRATION' OR conclusion = 'PASS');
ALTER TABLE certificates ADD CONSTRAINT cert_revoked_metadata
    CHECK (status = 'ACTIVE' OR (revoked_at IS NOT NULL AND revoked_reason IS NOT NULL));

CREATE INDEX idx_cert_device   ON certificates(device_id);
CREATE INDEX idx_cert_parent   ON certificates(parent_cert_no);
CREATE INDEX idx_cert_status   ON certificates(status);
CREATE INDEX idx_cert_created  ON certificates(created_at);

-- 同一设备“生效中(ACTIVE)”证书的有效期不得重叠。
-- 采用半开区间 [valid_from, valid_to)：首尾相接不算重叠。
-- 已撤销/已失效的证书释放区间，允许重新签发。
-- 该约束由数据库强制，即使高并发下也不会产生重叠证书。
ALTER TABLE certificates
    ADD CONSTRAINT cert_active_no_overlap
    EXCLUDE USING gist (
        device_id WITH =,
        tstzrange(valid_from, valid_to, '[)') WITH &&
    ) WHERE (status = 'ACTIVE');

-- ----------------------------------------------------------------------------
-- 撤销批次表：一次主动撤销对应一条记录
-- ----------------------------------------------------------------------------
CREATE TABLE revocations (
    batch_id     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cert_no      TEXT NOT NULL REFERENCES certificates(cert_no),
    reason       TEXT NOT NULL,
    revoked_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    operator     TEXT
);
CREATE INDEX idx_revocations_cert ON revocations(cert_no);

-- ----------------------------------------------------------------------------
-- 级联失效明细表：一次撤销导致哪些下游证书失效，及其完整引用链快照
-- chain 按 根 -> ... -> 失效证书 顺序存储证书编号，不可改写，用于追溯回放
-- ----------------------------------------------------------------------------
CREATE TABLE invalidations (
    batch_id     BIGINT NOT NULL REFERENCES revocations(batch_id),
    cert_no      TEXT NOT NULL REFERENCES certificates(cert_no),
    chain        TEXT[] NOT NULL,
    position     INT NOT NULL,   -- 失效证书在 chain 中的下标（从 1 开始）
    invalidated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, cert_no)
);

-- ----------------------------------------------------------------------------
-- 签发批次表：离线采集交接批次的一次性签发记录
-- batch_key 为幂等键；request/request_hash 为规范化请求快照及其指纹，
-- response 为首次签发结果快照——同键同请求重试时原样返回（即使证书事后被撤销），
-- 同键不同请求按 request_hash 判定为冲突。本表同样只增不改。
-- ----------------------------------------------------------------------------
CREATE TABLE issue_batches (
    batch_key    TEXT PRIMARY KEY,                 -- 批次键（幂等键，业务唯一）
    request_hash TEXT NOT NULL,                    -- 规范化请求的 SHA-256 指纹
    request      JSONB NOT NULL,                   -- 规范化请求快照
    response     JSONB NOT NULL,                   -- 首次签发结果快照（重试原样返回）
    cert_nos     TEXT[] NOT NULL,                  -- 本批签发的证书编号（按请求顺序）
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------------------------------
-- 触发器 1：证书签发内容不可改写
-- 仅允许 status / revoked_reason / revoked_at 在撤销事务中变化；
-- 其余业务字段（编号、设备、时间、结论、引用关系等）一律拒绝 UPDATE。
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION certificates_immutable() RETURNS trigger AS $$
BEGIN
    IF NEW.cert_no        IS DISTINCT FROM OLD.cert_no
       OR NEW.kind           IS DISTINCT FROM OLD.kind
       OR NEW.device_id      IS DISTINCT FROM OLD.device_id
       OR NEW.device_name    IS DISTINCT FROM OLD.device_name
       OR NEW.calibrated_at  IS DISTINCT FROM OLD.calibrated_at
       OR NEW.valid_from     IS DISTINCT FROM OLD.valid_from
       OR NEW.valid_to       IS DISTINCT FROM OLD.valid_to
       OR NEW.conclusion     IS DISTINCT FROM OLD.conclusion
       OR NEW.parent_cert_no IS DISTINCT FROM OLD.parent_cert_no
       OR NEW.operator       IS DISTINCT FROM OLD.operator
       OR NEW.remark         IS DISTINCT FROM OLD.remark
       OR NEW.created_at     IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'certificate % is sealed: issued content cannot be modified', OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    -- 状态只允许 ACTIVE -> REVOKED / INVALID，且必须由撤销事务写入
    IF NEW.status NOT IN ('REVOKED', 'INVALID') OR OLD.status <> 'ACTIVE' THEN
        RAISE EXCEPTION 'illegal status transition % -> % on certificate %',
            OLD.status, NEW.status, OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_certificates_immutable
    BEFORE UPDATE ON certificates
    FOR EACH ROW EXECUTE FUNCTION certificates_immutable();

-- ----------------------------------------------------------------------------
-- 触发器 2：证书不得物理删除（历史签发内容始终可查）
-- ----------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION certificates_no_delete() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'certificates cannot be deleted; revoke instead (cert_no=%)', OLD.cert_no
        USING ERRCODE = 'check_violation';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_certificates_no_delete
    BEFORE DELETE ON certificates
    FOR EACH ROW EXECUTE FUNCTION certificates_no_delete();

-- ----------------------------------------------------------------------------
-- 查询视图：统一将枚举以文本形式暴露给应用层
-- ----------------------------------------------------------------------------
CREATE VIEW v_certificates AS
SELECT cert_no,
       kind::text          AS kind,
       device_id,
       device_name,
       calibrated_at,
       valid_from,
       valid_to,
       conclusion::text    AS conclusion,
       parent_cert_no,
       operator,
       remark,
       status::text        AS status,
       revoked_reason,
       revoked_at,
       created_at
FROM certificates;
