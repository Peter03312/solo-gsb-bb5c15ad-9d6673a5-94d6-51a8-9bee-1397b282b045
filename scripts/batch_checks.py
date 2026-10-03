"""批次签发场景测试：整批原子性、批内乱序引用、幂等重放、键冲突与并发。

直接压 HTTP 接口（urllib，无第三方依赖）。服务需已启动。
用法: python3 scripts/batch_checks.py [base_url]
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


def d(days):
    return (t0 + timedelta(days=days)).isoformat()


def ref_item(no, dev, vf, vt, cal=None):
    return {
        "kind": "REFERENCE", "cert_no": no, "device_id": dev,
        "valid_from": vf, "valid_to": vt, "calibrated_at": cal or vf,
    }


def cal_item(no, dev, parent, vf, vt, cal, conclusion="PASS"):
    return {
        "kind": "CALIBRATION", "cert_no": no, "device_id": dev,
        "parent_cert_no": parent, "valid_from": vf, "valid_to": vt,
        "calibrated_at": cal, "conclusion": conclusion,
    }


def batch(key, certs):
    return call("POST", "/api/v1/certificate-batches",
                {"batch_key": key, "certificates": certs})


def err_code(body):
    return body.get("error", {}).get("code")


def err_cert(body):
    return body.get("error", {}).get("details", {}).get("cert_no")


print("== 0. 前置：库内基准证书（供批内引用库内证书的混合场景） ==")
s, _ = call("POST", "/api/v1/reference-certificates",
            {"cert_no": "BAT-DB-ROOT", "device_id": "DEV-BAT-DB",
             "valid_from": d(0), "valid_to": d(400), "calibrated_at": d(0)})
check("库内基准证书 201", s == 201, f"got {s}")

print("== A. 混合批次：列表乱序（子在前父在后），批内自含基准 ==")
certs_a = [
    cal_item("BAT-A-LEAF", "DEV-BAT-A2", "BAT-A-MID", d(150), d(400), d(151)),
    ref_item("BAT-A-ROOT", "DEV-BAT-A0", d(0), d(500)),
    cal_item("BAT-A-MID", "DEV-BAT-A1", "BAT-A-ROOT", d(30), d(300), d(31)),
]
s, body_a = batch("BAT-A", certs_a)
check("乱序批次签发 201", s == 201, json.dumps(body_a, ensure_ascii=False)[:300])
check("返回批次键与数量", body_a.get("batch_key") == "BAT-A" and body_a.get("count") == 3,
      json.dumps(body_a)[:200])
items = body_a.get("items", [])
check("items 按请求列表顺序返回",
      [i["certificate"]["cert_no"] for i in items]
      == ["BAT-A-LEAF", "BAT-A-ROOT", "BAT-A-MID"],
      str([i["certificate"]["cert_no"] for i in items]))
leaf = items[0] if items else {}
leaf_chain = [n["certificate"]["cert_no"] for n in leaf.get("chain", {}).get("nodes", [])]
check("叶子追溯链 基准->中间->叶子",
      leaf_chain == ["BAT-A-ROOT", "BAT-A-MID", "BAT-A-LEAF"], str(leaf_chain))
check("链起点为基准", leaf.get("chain", {}).get("root_cert_no") == "BAT-A-ROOT",
      str(leaf.get("chain", {}).get("root_cert_no")))

print("== B. 同键同请求重试：返回首次签发结果 ==")
s, body_b = batch("BAT-A", certs_a)
check("重试返回 200", s == 200, f"got {s}")
check("重试响应与首次完全一致", body_b == body_a,
      json.dumps(body_b, ensure_ascii=False)[:200])

print("== C. 证书事后被撤销，重试仍返回首次签发快照 ==")
s, body_rev = call("POST", "/api/v1/certificates/BAT-A-MID/revoke",
                   {"reason": "批次回放验证", "operator": "qa"})
check("撤销批内中间证书 200", s == 200, json.dumps(body_rev)[:200])
s, body_c = batch("BAT-A", certs_a)
check("撤销后同键重试仍 200", s == 200, f"got {s}")
mid = next((i for i in body_c.get("items", [])
            if i["certificate"]["cert_no"] == "BAT-A-MID"), {})
check("快照中状态仍为 ACTIVE（首次签发结果）",
      mid.get("certificate", {}).get("status") == "ACTIVE",
      mid.get("certificate", {}).get("status", ""))
s, body_now = call("GET", "/api/v1/certificates/BAT-A-MID")
check("现实状态已变为 REVOKED（快照不影响现态）",
      s == 200 and body_now.get("status") == "REVOKED", body_now.get("status", ""))

print("== D. 同键不同请求：报冲突 ==")
s, body_d = batch("BAT-A", [ref_item("BAT-A-OTHER", "DEV-BAT-A9", d(0), d(100))])
check("同键不同请求 409 batch_conflict",
      s == 409 and err_code(body_d) == "batch_conflict",
      f"{s} {json.dumps(body_d, ensure_ascii=False)[:200]}")

print("== E. 批内编号重复：整批拒绝并定位编号 ==")
s, body_e = batch("BAT-E", [
    ref_item("BAT-E-X", "DEV-BAT-E1", d(0), d(100)),
    ref_item("BAT-E-X", "DEV-BAT-E2", d(200), d(300)),
])
check("批内重复 422 batch_cert_duplicate",
      s == 422 and err_code(body_e) == "batch_cert_duplicate",
      f"{s} {json.dumps(body_e, ensure_ascii=False)[:200]}")
check("定位出错编号", err_cert(body_e) == "BAT-E-X", str(err_cert(body_e)))

print("== F. 批内引用成环：整批拒绝 ==")
s, body_f1 = batch("BAT-F1", [
    cal_item("BAT-F-A", "DEV-BAT-F1", "BAT-F-B", d(10), d(100), d(11)),
    cal_item("BAT-F-B", "DEV-BAT-F2", "BAT-F-A", d(10), d(100), d(11)),
])
check("双向成环 422 reference_cycle",
      s == 422 and err_code(body_f1) == "reference_cycle",
      f"{s} {json.dumps(body_f1, ensure_ascii=False)[:200]}")
s, body_f2 = batch("BAT-F2", [
    cal_item("BAT-F-S", "DEV-BAT-F3", "BAT-F-S", d(10), d(100), d(11)),
])
check("自引用成环 422 reference_cycle",
      s == 422 and err_code(body_f2) == "reference_cycle",
      f"{s} {json.dumps(body_f2, ensure_ascii=False)[:200]}")

print("== G. 引用不存在：整批拒绝并定位 ==")
s, body_g = batch("BAT-G", [
    ref_item("BAT-G-OK", "DEV-BAT-G1", d(0), d(100)),
    cal_item("BAT-G-BAD", "DEV-BAT-G2", "NOT-EXIST", d(10), d(90), d(11)),
])
check("未知引用 422 invalid_reference",
      s == 422 and err_code(body_g) == "invalid_reference",
      f"{s} {json.dumps(body_g, ensure_ascii=False)[:200]}")
check("定位出错编号为引用方", err_cert(body_g) == "BAT-G-BAD", str(err_cert(body_g)))
s, _ = call("GET", "/api/v1/certificates/BAT-G-OK")
check("整批不落库：合规记录也未入库", s == 404, f"got {s}")
s, _ = call("GET", "/api/v1/certificate-batches/BAT-G")
check("整批不落库：批次键未占用（可修正后重发）", s == 404, f"got {s}")

print("== H. 引用已撤销证书：整批拒绝 ==")
s, body_h = batch("BAT-H", [
    cal_item("BAT-H-1", "DEV-BAT-H1", "BAT-A-MID", d(40), d(200), d(41)),
])
check("引用已撤销证书 422 reference_not_active",
      s == 422 and err_code(body_h) == "reference_not_active",
      f"{s} {json.dumps(body_h, ensure_ascii=False)[:200]}")
check("定位出错编号", err_cert(body_h) == "BAT-H-1", str(err_cert(body_h)))

print("== I. 批内同设备有效期重叠：整批拒绝 ==")
s, body_i = batch("BAT-I", [
    ref_item("BAT-I-2", "DEV-BAT-I", d(50), d(150)),
    ref_item("BAT-I-1", "DEV-BAT-I", d(0), d(100)),
])
check("批内重叠 422 validity_overlap",
      s == 422 and err_code(body_i) == "validity_overlap",
      f"{s} {json.dumps(body_i, ensure_ascii=False)[:200]}")
check("定位出错编号（拓扑序后处理的一张）", err_cert(body_i) == "BAT-I-2",
      str(err_cert(body_i)))

print("== J. 与库内已有编号冲突：整批拒绝 ==")
s, body_j = batch("BAT-J", [
    ref_item("BAT-DB-ROOT", "DEV-BAT-J", d(0), d(100)),
])
check("编号冲突 409 cert_duplicate",
      s == 409 and err_code(body_j) == "cert_duplicate",
      f"{s} {json.dumps(body_j, ensure_ascii=False)[:200]}")
check("定位冲突编号", err_cert(body_j) == "BAT-DB-ROOT", str(err_cert(body_j)))

print("== K. 批内引用不合格上游：整批拒绝且不落库 ==")
s, body_k = batch("BAT-K", [
    cal_item("BAT-K-FAIL", "DEV-BAT-K1", "BAT-DB-ROOT", d(10), d(200), d(11),
             conclusion="FAIL"),
    cal_item("BAT-K-CHILD", "DEV-BAT-K2", "BAT-K-FAIL", d(20), d(100), d(21)),
])
check("引用不合格证书 422 reference_not_qualified",
      s == 422 and err_code(body_k) == "reference_not_qualified",
      f"{s} {json.dumps(body_k, ensure_ascii=False)[:200]}")
check("定位出错编号", err_cert(body_k) == "BAT-K-CHILD", str(err_cert(body_k)))
s, _ = call("GET", "/api/v1/certificates/BAT-K-FAIL")
check("不合格父证书也未落库", s == 404, f"got {s}")

print("== L. 校准时刻落在批内上游有效期外：整批拒绝 ==")
s, body_l = batch("BAT-L", [
    ref_item("BAT-L-ROOT", "DEV-BAT-L1", d(0), d(100)),
    cal_item("BAT-L-CHILD", "DEV-BAT-L2", "BAT-L-ROOT", d(150), d(250), d(151)),
])
check("校准时刻越界 422 reference_out_of_validity",
      s == 422 and err_code(body_l) == "reference_out_of_validity",
      f"{s} {json.dumps(body_l, ensure_ascii=False)[:200]}")
check("定位出错编号", err_cert(body_l) == "BAT-L-CHILD", str(err_cert(body_l)))

print("== M. 批内引用库内证书（混合批次） ==")
s, body_m = batch("BAT-M", [
    cal_item("BAT-M-1", "DEV-BAT-M1", "BAT-DB-ROOT", d(20), d(300), d(21)),
    cal_item("BAT-M-2", "DEV-BAT-M2", "BAT-M-1", d(30), d(200), d(31)),
])
check("混合批次 201", s == 201, json.dumps(body_m, ensure_ascii=False)[:200])
m2 = next((i for i in body_m.get("items", [])
           if i["certificate"]["cert_no"] == "BAT-M-2"), {})
m2_chain = [n["certificate"]["cert_no"] for n in m2.get("chain", {}).get("nodes", [])]
check("追溯链穿过库内基准", m2_chain == ["BAT-DB-ROOT", "BAT-M-1", "BAT-M-2"],
      str(m2_chain))

print("== N. 并发同键同请求：恰好一次首次签发，其余重放同一结果 ==")
certs_n = [
    ref_item("BAT-N-ROOT", "DEV-BAT-N0", d(0), d(400)),
    cal_item("BAT-N-MID", "DEV-BAT-N1", "BAT-N-ROOT", d(10), d(300), d(11)),
]
outcomes_n = []
lock = threading.Lock()


def worker_n(i):
    s, body = batch("BAT-N", certs_n)
    with lock:
        outcomes_n.append((i, s, body))


threads = [threading.Thread(target=worker_n, args=(i,)) for i in range(5)]
[t.start() for t in threads]
[t.join() for t in threads]
codes_n = sorted(o[1] for o in outcomes_n)
check("5 并发：1 个 201 + 4 个 200", codes_n == [200, 200, 200, 200, 201],
      f"codes={codes_n}")
bodies_n = {json.dumps(o[2], sort_keys=True, ensure_ascii=False) for o in outcomes_n}
check("全部并发响应内容一致", len(bodies_n) == 1, f"distinct={len(bodies_n)}")
s, body_nl = call("GET", "/api/v1/certificates/BAT-N-MID")
check("批内证书仅签发一张且生效", s == 200 and body_nl.get("status") == "ACTIVE",
      f"{s} {body_nl.get('status')}")

print("== O. 并发同键不同请求：一次成功，其余冲突 ==")
outcomes_o = []


def worker_o(i):
    s, body = batch("BAT-O", [
        ref_item(f"BAT-O-{i}", f"DEV-BAT-O{i}", d(0), d(100)),
    ])
    with lock:
        outcomes_o.append((i, s, body))


threads = [threading.Thread(target=worker_o, args=(i,)) for i in range(4)]
[t.start() for t in threads]
[t.join() for t in threads]
winners_o = [o for o in outcomes_o if o[1] == 201]
losers_o = [o for o in outcomes_o if o[1] != 201]
check("4 并发恰好 1 个 201", len(winners_o) == 1,
      f"codes={sorted(o[1] for o in outcomes_o)}")
check("其余均为 409 batch_conflict",
      len(losers_o) == 3
      and all(o[1] == 409 and err_code(o[2]) == "batch_conflict" for o in losers_o),
      json.dumps([(o[0], o[1], err_code(o[2])) for o in outcomes_o], ensure_ascii=False))

print("== P. 批次查询 ==")
s, body_p = call("GET", "/api/v1/certificate-batches/BAT-N")
check("查询批次 200 且与首次签发结果一致",
      s == 200 and body_p == outcomes_n[0][2],
      f"{s} {json.dumps(body_p, ensure_ascii=False)[:200]}")
s, body_p2 = call("GET", "/api/v1/certificate-batches/BAT-NOPE")
check("查询不存在批次 404 batch_not_found",
      s == 404 and err_code(body_p2) == "batch_not_found",
      f"{s} {json.dumps(body_p2, ensure_ascii=False)[:200]}")

print("== Q. 请求结构校验 ==")
s, _ = batch("BAT-Q-EMPTY", [])
check("空证书列表 422", s == 422, f"got {s}")
s, _ = call("POST", "/api/v1/certificate-batches", {"certificates": [
    ref_item("BAT-Q-1", "DEV-BAT-Q", d(0), d(100))]})
check("缺批次键 422", s == 422, f"got {s}")
s, _ = batch("BAT-Q-REFP", [
    {**ref_item("BAT-Q-2", "DEV-BAT-Q", d(0), d(100)), "parent_cert_no": "BAT-DB-ROOT"},
])
check("基准证书携带 parent_cert_no 422", s == 422, f"got {s}")
s, _ = batch("BAT-Q-NOP", [
    {"kind": "CALIBRATION", "cert_no": "BAT-Q-3", "device_id": "DEV-BAT-Q",
     "valid_from": d(0), "valid_to": d(100), "calibrated_at": d(1),
     "conclusion": "PASS"},
])
check("普通证书缺 parent_cert_no 422", s == 422, f"got {s}")

print("== R. 与单张签发互通：单张引用批次证书 ==")
s, body_r = call("POST", "/api/v1/certificates", {
    "cert_no": "BAT-R-SINGLE", "device_id": "DEV-BAT-R",
    "valid_from": d(40), "valid_to": d(200), "calibrated_at": d(41),
    "conclusion": "PASS", "parent_cert_no": "BAT-N-MID"})
check("单张签发引用批次证书 201", s == 201, json.dumps(body_r, ensure_ascii=False)[:200])
r_chain = [n["certificate"]["cert_no"] for n in body_r.get("chain", {}).get("nodes", [])]
check("追溯链穿过批次证书到基准",
      r_chain == ["BAT-N-ROOT", "BAT-N-MID", "BAT-R-SINGLE"], str(r_chain))

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
