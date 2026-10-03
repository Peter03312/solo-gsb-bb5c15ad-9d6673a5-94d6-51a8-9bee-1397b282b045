-- ============================================================================
-- 迁移：交接批次（issue_batches）整批签发能力
-- 适用于已运行旧版 db/init.sql 的库；幂等，可重复执行。
-- 用法: psql "$DB_URL" -f db/migrations/001_issue_batches.sql
-- ============================================================================
BEGIN;

CREATE TABLE IF NOT EXISTS issue_batches (
    batch_key       TEXT PRIMARY KEY,
    request_hash    TEXT NOT NULL,
    request_body    JSONB NOT NULL,
    result_snapshot JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    issued_at       TIMESTAMPTZ
);

ALTER TABLE certificates
    ADD COLUMN IF NOT EXISTS issue_batch_key TEXT REFERENCES issue_batches(batch_key) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_cert_batch ON certificates(issue_batch_key);

-- 交接批次封印触发器（见 db/init.sql 中同名定义）
CREATE OR REPLACE FUNCTION issue_batches_sealed() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'issue batch % cannot be deleted', OLD.batch_key
            USING ERRCODE = 'check_violation';
    END IF;
    IF OLD.result_snapshot IS NOT NULL
       OR NEW.result_snapshot IS NULL
       OR NEW.batch_key IS DISTINCT FROM OLD.batch_key
       OR NEW.request_hash IS DISTINCT FROM OLD.request_hash
       OR NEW.request_body IS DISTINCT FROM OLD.request_body
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'issue batch % is sealed: first issued result is immutable',
            OLD.batch_key USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_issue_batches_sealed ON issue_batches;
CREATE TRIGGER trg_issue_batches_sealed
    BEFORE UPDATE OR DELETE ON issue_batches
    FOR EACH ROW EXECUTE FUNCTION issue_batches_sealed();

-- 证书封印触发器：补充 issue_batch_key 字段。先替换函数再重建触发器。
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
       OR NEW.issue_batch_key IS DISTINCT FROM OLD.issue_batch_key
    THEN
        RAISE EXCEPTION 'certificate % is sealed: issued content cannot be modified', OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.status NOT IN ('REVOKED', 'INVALID') OR OLD.status <> 'ACTIVE' THEN
        RAISE EXCEPTION 'illegal status transition % -> % on certificate %',
            OLD.status, NEW.status, OLD.cert_no
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 视图重建以暴露 issue_batch_key（CREATE OR REPLACE VIEW 要求列兼容，故先 DROP）
DROP VIEW IF EXISTS v_certificates;
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
       created_at,
       issue_batch_key
FROM certificates;

COMMIT;
