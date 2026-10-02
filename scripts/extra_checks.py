"""补充场景测试：并发签发、边界区间、中段撤销、不可改写/删除。

直接压 HTTP 接口（urllib，无第三方依赖）。
用法: python3 scripts/extra_checks.py [base_url]
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8080"
results = []


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)


def iso(dt):
    return dt.isoformat()


def ref(no, dev, vf, vt, cal=None):
    return call("POST", "/api/v1/reference-certificates", {
        "cert_no": no, "device_id": dev,
        "valid_from": iso(vf), "valid_to": iso(vt),
        "calibrated_at": iso(cal or vf),
    })


def cal(no, dev, parent, vf, vt, cal, conclusion="PASS"):
    return call("POST", "/api/v1/certificates", {
        "cert_no": no, "device_id": dev, "parent_cert_no": parent,
        "valid_from": iso(vf), "valid_to": iso(vt),
        "calibrated_at": iso(cal), "conclusion": conclusion,
    })


print("== A. 首尾相接的有效期允许共存（半开区间） ==")
s, _ = ref("REF-A1", "DEV-A", t0, t0 + timedelta(days=180))
check("第一段基准证书 201", s == 201, f"got {s}")
s, body = ref("REF-A2", "DEV-A", t0 + timedelta(days=180), t0 + timedelta(days=365))
check("首尾相接第二段 201", s == 201, json.dumps(body)[:200])
s, _ = ref("REF-A3", "DEV-A", t0 + timedelta(days=100), t0 + timedelta(days=200))
check("真正重叠仍被拒 422", s == 422, f"got {s}")

print("== B. 并发重复签发：同设备重叠区间，仅一张成功 ==")
s, _ = ref("REF-B0", "DEV-B", t0, t0 + timedelta(days=400))
check("前置基准证书 201", s == 201, f"got {s}")

outcomes = []
lock = threading.Lock()


def worker(i):
    # 三个线程签发同设备、完全重叠的普通证书，引用同一张基准
    s, body = cal(
        f"CAL-B{i}", "DEV-BX", "REF-B0",
        t0 + timedelta(days=10), t0 + timedelta(days=200),
        t0 + timedelta(days=11),
    )
    with lock:
        outcomes.append((i, s, body))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(3)]
[t.start() for t in threads]
[t.join() for t in threads]
codes = sorted(o[1] for o in outcomes)
winners = [o for o in outcomes if o[1] == 201]
losers = [o for o in outcomes if o[1] != 201]
check("3 并发恰好 1 张成功", len(winners) == 1, f"codes={codes}")
check("其余 2 张被明确拒绝（422 预检或 409 约束兜底）",
      len(losers) == 2 and all(o[1] in (409, 422) for o in losers)
      and all(o[2]["error"]["code"] in
              ("validity_overlap", "validity_overlap_concurrent") for o in losers),
      json.dumps([(o[0], o[1], o[2].get("error", {}).get("code")) for o in outcomes], ensure_ascii=False))

print("== C. 并发相同证书编号：不产生脏数据 ==")
outcomes2 = []


def worker2(i):
    s, body = ref("REF-DUP", "DEV-DUP", t0, t0 + timedelta(days=30))
    with lock:
        outcomes2.append((i, s, body))


threads = [threading.Thread(target=worker2, args=(i,)) for i in range(3)]
[t.start() for t in threads]
[t.join() for t in threads]
codes2 = sorted(o[1] for o in outcomes2)
check("同编号并发仅 1 张成功", codes2.count(201) == 1 and codes2.count(409) == 2, f"codes={codes2}")

print("== D. 中段撤销：下游失效、上游不受影响，区间释放可重签 ==")
s, _ = ref("REF-D", "DEV-DR", t0, t0 + timedelta(days=400))
check("D 基准 201", s == 201, f"got {s}")
s, _ = cal("CAL-D1", "DEV-D1", "REF-D", t0 + timedelta(days=5), t0 + timedelta(days=300),
           t0 + timedelta(days=6))
check("D 二级 201", s == 201, f"got {s}")
s, _ = cal("CAL-D2", "DEV-D2", "CAL-D1", t0 + timedelta(days=10), t0 + timedelta(days=250),
           t0 + timedelta(days=11))
check("D 三级 201", s == 201, f"got {s}")

s, body = call("POST", "/api/v1/certificates/CAL-D1/revoke",
               {"reason": "中间标准器超差", "operator": "qa"})
check("撤销二级证书 200", s == 200, json.dumps(body)[:200])
check("仅三级证书受影响", body.get("affected_count") == 1
      and body["affected"][0]["cert_no"] == "CAL-D2", json.dumps(body)[:300])
chain_nos = [c["cert_no"] for c in body["affected"][0]["chain"]]
check("返回完整引用链 REF-D->CAL-D1->CAL-D2", chain_nos == ["REF-D", "CAL-D1", "CAL-D2"], str(chain_nos))

s, body = call("GET", "/api/v1/certificates/REF-D")
check("上游基准仍 ACTIVE", s == 200 and body["status"] == "ACTIVE", body.get("status"))
s, body = call("GET", "/api/v1/certificates/CAL-D1")
check("被撤销证书为 REVOKED 且内容保留", s == 200 and body["status"] == "REVOKED"
      and body["device_id"] == "DEV-D1" and body["revoked_reason"], json.dumps(body)[:200])
s, body = call("GET", "/api/v1/certificates/CAL-D2")
check("下游证书为 INVALID 且历史内容保留", s == 200 and body["status"] == "INVALID"
      and body["parent_cert_no"] == "CAL-D1", json.dumps(body)[:200])

# CAL-D1 失效后引用它必须被拒绝
s, body = cal("CAL-D3", "DEV-D3", "CAL-D1", t0 + timedelta(days=20), t0 + timedelta(days=100),
              t0 + timedelta(days=21))
check("失效证书不能再作依据 422", s == 422 and body["error"]["code"] == "reference_not_active",
      json.dumps(body)[:200])

# 撤销释放了 DEV-D1 的区间，可以重新签发同设备证书
s, body = cal("CAL-D1-NEW", "DEV-D1", "REF-D", t0 + timedelta(days=5), t0 + timedelta(days=300),
              t0 + timedelta(days=6))
check("撤销后同设备同区间可重签新证书 201", s == 201, json.dumps(body)[:200])

# 撤销批次可查
s, body = call("GET", "/api/v1/revocations/1")
check("撤销批次详情可查", s == 200 and body["batch_id"] == 1
      and len(body["affected"]) >= 1, json.dumps(body)[:200])

print("== E. 不合格普通证书不得被引用 ==")
s, _ = cal("CAL-F", "DEV-F", "REF-D", t0 + timedelta(days=30), t0 + timedelta(days=200),
           t0 + timedelta(days=31), conclusion="FAIL")
check("不合格普通证书可签发 201", s == 201, f"got {s}")
s, body = cal("CAL-F-USE", "DEV-F2", "CAL-F", t0 + timedelta(days=40), t0 + timedelta(days=150),
              t0 + timedelta(days=41))
check("引用不合格证书被拒 422", s == 422 and body["error"]["code"] == "reference_not_qualified",
      json.dumps(body)[:200])

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
