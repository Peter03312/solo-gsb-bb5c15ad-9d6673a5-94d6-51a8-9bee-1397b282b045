#!/usr/bin/env bash
# 端到端冒烟测试：服务已启动后运行（BASE_URL 可覆盖）
# 用法: scripts/smoke-test.sh [BASE_URL]
set -u
BASE="${1:-http://localhost:8080}"
CURL="curl -sS -H Content-Type:application/json"
pass=0; fail=0

ok()   { echo "  [PASS] $1"; pass=$((pass+1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail+1)); }

# expect_status <预期码> <实际码> <描述>
expect() {
  if [ "$1" = "$2" ]; then ok "$3 (HTTP $2)"; else bad "$3 (期望 $1 实际 $2)"; fi;
}

echo "== 0. 健康检查 =="
expect 200 "$($CURL -o /dev/null -w '%{http_code}' "$BASE/health")" "健康检查"

echo "== 1. 登记基准设备证书（追溯起点） =="
expect 201 "$($CURL -o /tmp/st_ref.json -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-001","device_id":"DEV-MASTER","device_name":"实验室最高标准器",
  "valid_from":"2026-01-01T00:00:00+08:00","valid_to":"2027-01-01T00:00:00+08:00",
  "calibrated_at":"2026-01-01T09:00:00+08:00","operator":"张工","remark":"年度基准登记"}')" "登记基准证书"

echo "== 2. 签发普通证书（引用基准） =="
expect 201 "$($CURL -o /tmp/st_c1.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-WS-001","device_id":"DEV-WS-A","device_name":"车间万用表A",
  "valid_from":"2026-03-01T00:00:00+08:00","valid_to":"2027-03-01T00:00:00+08:00",
  "calibrated_at":"2026-03-05T10:00:00+08:00","conclusion":"PASS",
  "parent_cert_no":"REF-MASTER-001","operator":"李工"}')" "签发二级证书"

expect 201 "$($CURL -o /tmp/st_c2.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-CUS-001","device_id":"DEV-CUS-B","device_name":"客户仪表B",
  "valid_from":"2026-04-01T00:00:00+08:00","valid_to":"2026-10-01T00:00:00+08:00",
  "calibrated_at":"2026-04-02T10:00:00+08:00","conclusion":"PASS",
  "parent_cert_no":"CAL-WS-001","operator":"王工"}')" "签发三级证书"

echo "== 3. 无效引用必须被拒绝 =="
expect 422 "$($CURL -o /tmp/st_e1.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-001","device_id":"DEV-E","valid_from":"2026-05-01T00:00:00+08:00",
  "valid_to":"2026-11-01T00:00:00+08:00","calibrated_at":"2026-05-01T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"NOT-EXIST"}')" "引用不存在的证书 -> 422"

expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-002","device_id":"DEV-E","valid_from":"2028-01-01T00:00:00+08:00",
  "valid_to":"2028-07-01T00:00:00+08:00","calibrated_at":"2028-01-02T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"REF-MASTER-001"}')" "校准时刻超出基准有效期 -> 422"

echo "== 4. 有效期冲突必须被拒绝 =="
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-002","device_id":"DEV-MASTER",
  "valid_from":"2026-06-01T00:00:00+08:00","valid_to":"2026-12-01T00:00:00+08:00",
  "calibrated_at":"2026-06-01T09:00:00+08:00"}')" "同设备生效区间重叠 -> 422"

echo "== 5. 重复证书编号必须被拒绝 =="
expect 409 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/reference-certificates" -d '{
  "cert_no":"REF-MASTER-001","device_id":"DEV-OTHER",
  "valid_from":"2027-02-01T00:00:00+08:00","valid_to":"2028-02-01T00:00:00+08:00",
  "calibrated_at":"2027-02-01T09:00:00+08:00"}')" "证书编号重复 -> 409"

echo "== 6. 追溯链查询 =="
expect 200 "$($CURL -o /tmp/st_chain.json -w '%{http_code}' "$BASE/api/v1/certificates/CAL-CUS-001/chain")" "查询追溯链"
grep -q '"length":3' /tmp/st_chain.json && ok "追溯链长度为 3" || bad "追溯链长度"

echo "== 7. 撤销基准证书，级联失效下游 =="
expect 200 "$($CURL -o /tmp/st_rev.json -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-MASTER-001/revoke" -d '{
  "reason":"年度比对发现超差","operator":"质量负责人"}')" "撤销基准证书"
grep -q '"affected_count":2' /tmp/st_rev.json && ok "受影响证书数量 2" || bad "受影响证书数量"
grep -q CAL-WS-001 /tmp/st_rev.json && grep -q CAL-CUS-001 /tmp/st_rev.json \
  && ok "返回受影响证书及引用链" || bad "受影响证书内容"

echo "== 8. 失效证书不得再作校准依据 =="
expect 422 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates" -d '{
  "cert_no":"CAL-E-003","device_id":"DEV-E","valid_from":"2026-05-01T00:00:00+08:00",
  "valid_to":"2026-11-01T00:00:00+08:00","calibrated_at":"2026-05-01T09:00:00+08:00",
  "conclusion":"PASS","parent_cert_no":"CAL-WS-001"}')" "引用已失效证书 -> 422"

expect 409 "$($CURL -o /dev/null -w '%{http_code}' -X POST "$BASE/api/v1/certificates/REF-MASTER-001/revoke" -d '{
  "reason":"重复撤销"}')" "重复撤销 -> 409"

echo "== 9. 历史内容仍可查 =="
expect 200 "$($CURL -o /tmp/st_hist.json -w '%{http_code}' "$BASE/api/v1/devices/DEV-CUS-B/certificates")" "设备历史可查"
grep -q '"status":"INVALID"' /tmp/st_hist.json && ok "历史证书状态为 INVALID 且内容保留" || bad "历史内容"

echo
echo "结果: PASS=$pass FAIL=$fail"
[ "$fail" -eq 0 ]
