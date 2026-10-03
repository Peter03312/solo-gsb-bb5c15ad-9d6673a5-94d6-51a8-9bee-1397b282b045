"""交接批次（issue-batches）整批签发专项检查。

直接压 HTTP 接口（urllib，无第三方依赖）。
用法: python3 scripts/batch_checks.py [base_url]

覆盖：
  A. 正常整批（批内基准/多级引用、乱序）与追溯链
  B. 普通证书引用库内现有证书
  C. 同键同请求重试（列表顺序无关）返回首次结果，HTTP 200 + 重放标记
  D. 同键不同请求 -> 409 issue_batch_conflict
  E. 撤销后重试仍返回首次快照；批次详情/列表反映当前状态
  F. 各类不合规：批内编号重复/与库冲突/字段组合/引用无效/越界/成环/重叠，
     任一不合规则整批不落库（出错编号可定位、整批证书均查不到）
  G. 校验失败后同键可重试成功（占位行随回滚消失）
  H. 并发同键重复提交：仅 1 次 201，其余 200 重放，签发结果一致
  I. 批次与单张签发并发抢同一编号：恰好一方成功，无脏数据
  J. 批次与撤销并发：不存在 ACTIVE 证书引用非 ACTIVE 上游
  K. 批次列表/详情
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
            return r.status, dict(r.headers), json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), json.loads(e.read())


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + ("" if cond else f" :: {detail}"))


TZ = timezone(timedelta(hours=8))
t0 = datetime(2026, 1, 1, 0, 0, tzinfo=TZ)


def iso(dt):
    return dt.isoformat()


def ref_item(no, dev, vf=None, vt=None, cal=None, name=None, operator=None, remark=None):
    return {
        "cert_no": no, "kind": "REFERENCE", "device_id": dev,
        "device_name": name or dev,
        "valid_from": iso(vf or t0), "valid_to": iso(vt or t0 + timedelta(days=400)),
        "calibrated_at": iso(cal or vf or t0),
        "operator": operator, "remark": remark,
    }


def cal_item(no, dev, parent, vf=None, vt=None, cal=None, conclusion="PASS"):
    # 默认自身有效期 [t0+1d, t0+400d)，显式 cal 落在其中
    vf = vf or t0 + timedelta(days=1)
    vt = vt or t0 + timedelta(days=400)
    return {
        "cert_no": no, "kind": "CALIBRATION", "device_id": dev,
        "device_name": dev, "valid_from": iso(vf), "valid_to": iso(vt),
        "calibrated_at": iso(cal or vf + timedelta(days=1)),
        "conclusion": conclusion, "parent_cert_no": parent,
    }


def batch(key, items):
    return call("POST", "/api/v1/issue-batches", {"batch_key": key, "certificates": items})


def absent(*nos):
    """整批失败后所有编号都不应落库。"""
    return all(call("GET", f"/api/v1/certificates/{n}")[0] == 404 for n in nos)


# ---------------------------------------------------------------------------
print("== A. 正常整批：批内基准 + 多级引用（乱序），追溯链正确 ==")
items_a = [
    cal_item("A-CHILD", "DEV-A3", "A-MID",
             vf=t0 + timedelta(days=60), vt=t0 + timedelta(days=200),
             cal=t0 + timedelta(days=61)),
    ref_item("A-REF", "DEV-A1"),
    cal_item("A-MID", "DEV-A2", "A-REF",
             vf=t0 + timedelta(days=30), vt=t0 + timedelta(days=300),
             cal=t0 + timedelta(days=31)),
]
s, hdr, ba = batch("KEY-A", items_a)
b = ba
check("整批签发 201", s == 201, json.dumps(b)[:300])
check("cert_count=3 replayed=False", b.get("cert_count") == 3 and b.get("replayed") is False, str(b)[:200])
by_no = {c["cert_no"]: c for c in b.get("certificates", [])}
check("A-MID 链 A-REF->A-MID",
      [n["certificate"]["cert_no"] for n in by_no["A-MID"]["chain"]["nodes"]] == ["A-REF", "A-MID"])
check("A-CHILD 链 A-REF->A-MID->A-CHILD",
      [n["certificate"]["cert_no"] for n in by_no["A-CHILD"]["chain"]["nodes"]]
      == ["A-REF", "A-MID", "A-CHILD"])
check("结果与请求列表同序", [c["cert_no"] for c in b["certificates"]] == ["A-CHILD", "A-REF", "A-MID"])
check("证书回带批次键", all(c["certificate"]["issue_batch_key"] == "KEY-A" for c in b["certificates"]))

# ---------------------------------------------------------------------------
print("== B. 普通证书引用库内已存在的单张基准 ==")
s, _, b = call("POST", "/api/v1/reference-certificates", {
    "cert_no": "LIB-REF", "device_id": "DEV-LIB", "device_name": "库内基准",
    "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=400)),
    "calibrated_at": iso(t0 + timedelta(days=2)),
})
check("预置库内基准 201", s == 201, str(s))
s, _, b = batch("KEY-B", [cal_item("B-CHILD", "DEV-B1", "LIB-REF",
                                   cal=t0 + timedelta(days=10))])
check("引用库内证书整批签发 201", s == 201, json.dumps(b)[:300])
check("链根为库内基准", b["certificates"][0]["chain"]["root_cert_no"] == "LIB-REF")

# ---------------------------------------------------------------------------
print("== C. 同键同请求重试（调换列表顺序）-> 200 重放首次结果 ==")
s, hdr, b2 = batch("KEY-A", list(reversed(items_a)))
check("重试返回 200", s == 200, str(s))
check("Idempotency-Replayed 头",
      hdr.get("Idempotency-Replayed") == "true"
      or next((v for k, v in hdr.items() if k.lower() == "idempotency-replayed"), None) == "true",
      str(hdr))
check("replayed=True 且 issued_at 与首次一致",
      b2.get("replayed") is True and b2["issued_at"] == ba["issued_at"],
      f"{b2.get('replayed')} {b2.get('issued_at')} vs {ba['issued_at']}")
# 直接对比两次响应的证书内容与链完全一致
sorted_first = sorted(ba["certificates"], key=lambda c: c["cert_no"])
sorted_replay = sorted(b2["certificates"], key=lambda c: c["cert_no"])
check("重放结果证书/链与首次完全一致",
      json.dumps(sorted_first, sort_keys=True) == json.dumps(sorted_replay, sort_keys=True))

# ---------------------------------------------------------------------------
print("== D. 同键不同请求 -> 409 issue_batch_conflict ==")
s, _, b = batch("KEY-A", [ref_item("A-DIFFERENT", "DEV-DIFF")])
check("冲突 409", s == 409 and b["error"]["code"] == "issue_batch_conflict",
      f"{s} {json.dumps(b)[:200]}")
check("冲突证书未签发", call("GET", "/api/v1/certificates/A-DIFFERENT")[0] == 404)

# ---------------------------------------------------------------------------
print("== E. 撤销后同键重试仍返回首次快照（历史 ACTIVE），实时状态已失效 ==")
s, _, _ = call("POST", "/api/v1/certificates/A-REF/revoke", {"reason": "批次测试撤销"})
check("撤销批内基准 200", s == 200, str(s))
s, hdr, b = batch("KEY-A", items_a)
check("撤销后重试仍 200 重放", s == 200 and b["replayed"] is True, str(s))
snap_status = {c["cert_no"]: c["certificate"]["status"] for c in b["certificates"]}
check("快照中证书仍为首次的 ACTIVE", all(v == "ACTIVE" for v in snap_status.values()), str(snap_status))
s, _, live = call("GET", "/api/v1/certificates/A-MID")
check("实时查询 A-MID 已级联 INVALID", s == 200 and live["status"] == "INVALID", live.get("status"))
s, _, det = call("GET", "/api/v1/issue-batches/KEY-A")
det_status = {c["cert_no"]: c["certificate"]["status"] for c in det["certificates"]}
check("批次详情反映当前状态（非 ACTIVE）",
      s == 200 and set(det_status.values()) <= {"REVOKED", "INVALID"}, str(det_status))
s, _, lst = call("GET", "/api/v1/issue-batches")
check("批次列表含 KEY-A / KEY-B", s == 200 and
      {i["batch_key"] for i in lst["items"]} >= {"KEY-A", "KEY-B"}, json.dumps(lst)[:200])

# ---------------------------------------------------------------------------
print("== F. 任一记录不合规 -> 整批不落库，错误定位 index/cert_no/code ==")


def expect_batch_reject(key, items, code, *must_absent):
    s, _, b = batch(key, items)
    errs = b.get("error", {}).get("details", {}).get("errors", [])
    ok = s == 422 and b["error"]["code"] == "batch_validation_failed" \
        and any(e.get("code") == code for e in errs)
    check(f"{code} 被拒且定位编号", ok, f"{s} {json.dumps(b)[:260]}")
    check("出错项含 index/cert_no/message",
          all({"index", "cert_no", "code", "message"} <= set(e) for e in errs), str(errs)[:200])
    check("整批不落库", absent(*must_absent), str(must_absent))
    return errs


expect_batch_reject("K-F1", [
    cal_item("F1-A", "DEV-F1", "F1-B", cal=t0 + timedelta(days=5)),
    cal_item("F1-B", "DEV-F2", "LIB-REF", cal=t0 + timedelta(days=5)),
    cal_item("F1-A", "DEV-F3", "LIB-REF", cal=t0 + timedelta(days=5)),
], "batch_cert_no_duplicate", "F1-A", "F1-B")

expect_batch_reject("K-F2", [ref_item("LIB-REF", "DEV-X")],
                    "cert_duplicate")

expect_batch_reject("K-F3", [{
    **ref_item("F3-R", "DEV-F3"), "parent_cert_no": "LIB-REF"}],
    "reference_with_parent", "F3-R")

expect_batch_reject("K-F4", [{
    "cert_no": "F4-C", "kind": "CALIBRATION", "device_id": "DEV-F4",
    "valid_from": iso(t0 + timedelta(days=30)), "valid_to": iso(t0 + timedelta(days=200)),
    "calibrated_at": iso(t0 + timedelta(days=31)), "conclusion": "PASS"}],
    "invalid_reference", "F4-C")

expect_batch_reject("K-F5", [{
    "cert_no": "F5-C", "kind": "CALIBRATION", "device_id": "DEV-F5",
    "valid_from": iso(t0 + timedelta(days=30)), "valid_to": iso(t0 + timedelta(days=200)),
    "calibrated_at": iso(t0 + timedelta(days=31)), "parent_cert_no": "LIB-REF"}],
    "conclusion_required", "F5-C")

expect_batch_reject("K-F6", [{**ref_item("F6-R", "DEV-F6"), "conclusion": "FAIL"}],
                    "reference_not_qualified", "F6-R")

expect_batch_reject("K-F7", [cal_item("F7-C", "DEV-F7", "NO-SUCH-PARENT",
                                      cal=t0 + timedelta(days=5))],
                    "invalid_reference", "F7-C")

# 引用已撤销的库内证书
expect_batch_reject("K-F8", [cal_item("F8-C", "DEV-F8", "A-REF",
                                      cal=t0 + timedelta(days=5))],
                    "reference_not_active", "F8-C")

# 库内不合格上游
s, _, _ = batch("K-F9P", [
    cal_item("F9-FAIL", "DEV-F9", "LIB-REF", cal=t0 + timedelta(days=5), conclusion="FAIL")])
assert s == 201, s
expect_batch_reject("K-F9", [cal_item("F9-C", "DEV-F9B", "F9-FAIL",
                                      cal=t0 + timedelta(days=6))],
                    "reference_not_qualified", "F9-C")

# 校准时刻超出库内上游有效期（但落在自身窗口内）
expect_batch_reject("K-F10", [cal_item("F10-C", "DEV-F10", "LIB-REF",
                                       vt=t0 + timedelta(days=500),
                                       cal=t0 + timedelta(days=401))],
                    "reference_out_of_validity", "F10-C")

# 校准时刻超出批内上游有效期
expect_batch_reject("K-F11", [
    ref_item("F11-R", "DEV-F11R", vt=t0 + timedelta(days=100)),
    cal_item("F11-C", "DEV-F11C", "F11-R", cal=t0 + timedelta(days=200)),
], "reference_out_of_validity", "F11-R", "F11-C")

# 批内引用成环
expect_batch_reject("K-F12", [
    cal_item("F12-A", "DEV-x", "F12-B", cal=t0 + timedelta(days=5)),
    cal_item("F12-B", "DEV-y", "F12-C", cal=t0 + timedelta(days=5)),
    cal_item("F12-C", "DEV-z", "F12-A", cal=t0 + timedelta(days=5)),
], "reference_cycle", "F12-A", "F12-B", "F12-C")

# 自引用
expect_batch_reject("K-F13", [cal_item("F13-A", "DEV-x", "F13-A",
                                       cal=t0 + timedelta(days=5))],
                    "reference_cycle", "F13-A")

# 与库内 ACTIVE 有效期重叠（单张接口预置）
s, _, _ = call("POST", "/api/v1/reference-certificates", {
    "cert_no": "F14-LIB", "device_id": "DEV-F14",
    "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=200)),
    "calibrated_at": iso(t0 + timedelta(days=1))})
assert s == 201, s
expect_batch_reject("K-F14", [ref_item("F14-C", "DEV-F14",
                                       vf=t0 + timedelta(days=100), vt=t0 + timedelta(days=300))],
                    "validity_overlap", "F14-C")

# 批内同设备重叠：两个编号都应被定位
errs = expect_batch_reject("K-F15", [
    ref_item("F15-A", "DEV-F15", vf=t0, vt=t0 + timedelta(days=200)),
    ref_item("F15-B", "DEV-F15", vf=t0 + timedelta(days=100), vt=t0 + timedelta(days=300)),
], "validity_overlap", "F15-A", "F15-B")
overlap_nos = {e["cert_no"] for e in errs if e["code"] == "validity_overlap"}
check("重叠双方编号都被定位", {"F15-A", "F15-B"} <= overlap_nos, str(overlap_nos))

# 非空列表校验（Pydantic 422）
s, _, b = batch("K-F16", [])
check("空证书列表 422", s == 422, str(s))

# ---------------------------------------------------------------------------
print("== G. 校验失败后同键可被修正请求重试成功 ==")
good_g = [
    ref_item("G-REF", "DEV-G1"),
    cal_item("G-C", "DEV-G2", "G-REF", cal=t0 + timedelta(days=5)),
]
s, _, b = batch("KEY-G", [cal_item("G-C", "DEV-G2", "GHOST",
                                   cal=t0 + timedelta(days=5))])
check("先失败 422", s == 422, str(s))
s, _, b = batch("KEY-G", good_g)
check("同键修正后 201", s == 201, json.dumps(b)[:200])
s, _, b = batch("KEY-G", list(reversed(good_g)))
check("再重试 200 重放", s == 200 and b["replayed"] is True, str(s))

# ---------------------------------------------------------------------------
print("== H. 并发同键重复提交：恰好 1 次 201，其余 200 重放 ==")
items_h = [ref_item("H-R", "DEV-H1"), cal_item("H-C", "DEV-H2", "H-R",
                                               cal=t0 + timedelta(days=5))]
outcomes = []
lock = threading.Lock()


def worker_h(i):
    s, _, b = batch("KEY-H", items_h if i % 2 == 0 else list(reversed(items_h)))
    with lock:
        outcomes.append((i, s, b))


ths = [threading.Thread(target=worker_h, args=(i,)) for i in range(6)]
[t.start() for t in ths]
[t.join() for t in ths]
codes = sorted(o[1] for o in outcomes)
check("6 并发：1 个 201 + 5 个 200", codes.count(201) == 1 and codes.count(200) == 5, str(codes))
created = [o[2].get("issued_at") for o in outcomes if isinstance(o[2], dict)]
check("所有响应 issued_at 一致（同一首次结果）", len(set(created)) == 1, str(set(created)))
s, _, cert = call("GET", "/api/v1/certificates/H-R")
check("H-R 仅一张且 ACTIVE", s == 200 and cert["status"] == "ACTIVE", cert.get("status"))

# ---------------------------------------------------------------------------
print("== I. 批次与单张签发并发抢同一编号：恰好一方成功 ==")
outcomes_i = []


def worker_batch():
    s, _, b = batch("KEY-I", [ref_item("I-X", "DEV-IB")])
    with lock:
        outcomes_i.append(("batch", s, b))


def worker_single():
    s, _, b = call("POST", "/api/v1/reference-certificates", {
        "cert_no": "I-X", "device_id": "DEV-ISING",
        "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=400)),
        "calibrated_at": iso(t0)})
    with lock:
        outcomes_i.append(("single", s, b))


for rnd in range(3):
    no = f"I-X{rnd}"
    outcomes_i = []
    # 用循环内编号重定义闭包
    def wb(no=no):
        s, _, b = batch(f"KEY-I{rnd}", [ref_item(no, f"DEV-IB{rnd}")])
        with lock:
            outcomes_i.append(("batch", s, b))

    def ws(no=no, rnd=rnd):
        s, _, b = call("POST", "/api/v1/reference-certificates", {
            "cert_no": no, "device_id": f"DEV-ISING{rnd}",
            "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=400)),
            "calibrated_at": iso(t0)})
        with lock:
            outcomes_i.append(("single", s, b))

    t1, t2 = threading.Thread(target=wb), threading.Thread(target=ws)
    t1.start(); t2.start(); t1.join(); t2.join()
    batch_o = next(o for o in outcomes_i if o[0] == "batch")
    single_o = next(o for o in outcomes_i if o[0] == "single")
    wins = [o for o in (batch_o, single_o) if o[1] == 201]
    bcode = batch_o[2].get("error", {}).get("code") if isinstance(batch_o[2], dict) else None
    # 单张先抢到编号：批次可能整批 422(cert_duplicate) 或 409 并发兜底，均为原子拒绝
    batch_rejected = batch_o[1] in (409, 422) and bcode in (
        "cert_duplicate", "batch_validation_failed", "issue_batch_concurrent")
    batch_ok = batch_o[1] == 201 or batch_rejected
    check(f"轮次{rnd}：恰好一方成功且无脏数据",
          len(wins) == 1 and single_o[1] in (201, 409) and batch_ok,
          json.dumps([(o[0], o[1], o[2].get("error", {}).get("code") if isinstance(o[2], dict) else None)
                      for o in outcomes_i], ensure_ascii=False))

# ---------------------------------------------------------------------------
print("== J. 批次与撤销并发：不允许 ACTIVE 证书引用非 ACTIVE 上游 ==")
# 每轮使用独立基准，重复多轮提高竞态覆盖
bad = []
for rnd in range(5):
    rno = f"J-R{rnd}"
    cno = f"J-C{rnd}"
    s, _, _ = call("POST", "/api/v1/reference-certificates", {
        "cert_no": rno, "device_id": f"DEV-JR{rnd}",
        "valid_from": iso(t0), "valid_to": iso(t0 + timedelta(days=400)),
        "calibrated_at": iso(t0)})
    assert s == 201, s
    ob = {}

    def wbatch(rnd=rnd, rno=rno, cno=cno):
        s, _, b = batch(f"KEY-J{rnd}", [cal_item(cno, f"DEV-JC{rnd}", rno,
                                                 cal=t0 + timedelta(days=5))])
        ob["s"], ob["b"] = s, b

    def wrev(rno=rno):
        s, _, b = call("POST", f"/api/v1/certificates/{rno}/revoke",
                       {"reason": "并发撤销"})
        ob["rs"], ob["rb"] = s, b

    t1, t2 = threading.Thread(target=wbatch), threading.Thread(target=wrev)
    t1.start(); t2.start(); t1.join(); t2.join()
    _, _, rlive = call("GET", f"/api/v1/certificates/{rno}")
    cs_h, _, clive = call("GET", f"/api/v1/certificates/{cno}")
    if cs_h == 200 and clive["status"] == "ACTIVE" and rlive["status"] != "ACTIVE":
        bad.append(rnd)
check("5 轮批次/撤销并发均无 ACTIVE 引用失效上游", not bad, f"违例轮次 {bad}")

# ---------------------------------------------------------------------------
print("== K. 批次详情不存在 -> 404 ==")
s, _, b = call("GET", "/api/v1/issue-batches/NO-SUCH-BATCH")
check("未知批次 404", s == 404 and b["error"]["code"] == "issue_batch_not_found", str(s))

fails = [r for r in results if not r[1]]
print(f"\n结果: PASS={len(results) - len(fails)} FAIL={len(fails)}")
sys.exit(1 if fails else 0)
