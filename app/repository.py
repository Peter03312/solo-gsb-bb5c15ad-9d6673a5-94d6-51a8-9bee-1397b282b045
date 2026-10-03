"""数据访问与核心业务规则（全部在数据库事务中执行）。"""
import hashlib
import json

import psycopg

from .errors import ApiError

# v_certificates 视图的列顺序
_VIEW_COLS = (
    "cert_no, kind, device_id, device_name, calibrated_at, valid_from, valid_to, "
    "conclusion, parent_cert_no, operator, remark, status, revoked_reason, "
    "revoked_at, created_at, issue_batch_key"
)


# ---------------------------------------------------------------------------
# 公共查询
# ---------------------------------------------------------------------------

def _get_cert_row(conn, cert_no: str, lock: bool = False) -> dict | None:
    sql = f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = %s"
    if lock:
        # 视图基于单表，FOR UPDATE 会落到底层 certificates 行上
        sql += " FOR UPDATE"
    return conn.execute(sql, (cert_no,)).fetchone()


def get_certificate(conn, cert_no: str) -> dict:
    row = _get_cert_row(conn, cert_no)
    if row is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return row


def get_chain(conn, cert_no: str) -> dict:
    """返回从追溯起点（基准证书）到目标证书的完整引用链。"""
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")

    sql = f"""
        WITH RECURSIVE walk AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, w.depth - 1
            FROM certificates c JOIN walk w ON c.cert_no = w.parent_cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT w.depth, vc.cert_no, vc.kind, vc.device_id, vc.device_name,
               vc.calibrated_at, vc.valid_from, vc.valid_to, vc.conclusion,
               vc.parent_cert_no, vc.operator, vc.remark, vc.status,
               vc.revoked_reason, vc.revoked_at, vc.created_at
        FROM walk w JOIN v_certificates vc ON vc.cert_no = w.cert_no
        WHERE NOT w.is_cycle
        ORDER BY w.depth ASC, vc.cert_no ASC
    """
    nodes = [
        {"depth": r.pop("depth"), "certificate": r}
        for r in conn.execute(sql, (cert_no,)).fetchall()
    ]
    if not nodes:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    return {
        "cert_no": cert_no,
        "nodes": nodes,
        "root_cert_no": nodes[0]["certificate"]["cert_no"],
        "length": len(nodes),
    }


def list_certificates(
    conn,
    device_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    where, params = [], []
    if device_id:
        where.append("device_id = %s")
        params.append(device_id)
    if status:
        where.append("status = %s")
        params.append(status)
    if kind:
        where.append("kind = %s")
        params.append(kind)
    clause = ("WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT count(*) AS n FROM certificates {clause}", params
    ).fetchone()["n"]
    rows = conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates {clause} "
        f"ORDER BY created_at DESC, cert_no ASC LIMIT %s OFFSET %s",
        (*params, limit, offset),
    ).fetchall()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


def get_device_history(conn, device_id: str) -> list[dict]:
    seen = conn.execute(
        "SELECT 1 FROM certificates WHERE device_id = %s LIMIT 1", (device_id,)
    ).fetchone()
    if seen is None:
        raise ApiError(404, "device_not_found", f"设备无任何证书记录: {device_id}")
    return conn.execute(
        f"SELECT {_VIEW_COLS} FROM v_certificates WHERE device_id = %s "
        f"ORDER BY calibrated_at DESC, created_at DESC",
        (device_id,),
    ).fetchall()


def get_downstream(conn, cert_no: str) -> dict:
    if _get_cert_row(conn, cert_no) is None:
        raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, status
            FROM certificates WHERE parent_cert_no = %s
            UNION ALL
            SELECT c.cert_no, d.depth + 1, c.status
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, status::text AS status
        FROM d WHERE NOT is_cycle
        ORDER BY depth ASC, cert_no ASC
    """
    return {"cert_no": cert_no, "descendants": conn.execute(sql, (cert_no,)).fetchall()}


# ---------------------------------------------------------------------------
# 证书签发
# ---------------------------------------------------------------------------

def _validate_parent(conn, parent_no: str, calibrated_at) -> dict:
    """被引用的标准设备证书：必须存在、生效中、合格，且校准时刻落在有效期内。"""
    parent = _get_cert_row(conn, parent_no, lock=True)
    if parent is None:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    if parent["status"] != "ACTIVE":
        raise ApiError(
            422,
            "reference_not_active",
            f"引用的标准证书 {parent_no} 当前状态为 {parent['status']}，"
            "已撤销/失效证书不得作为校准依据",
        )
    if parent["conclusion"] != "PASS":
        raise ApiError(
            422,
            "reference_not_qualified",
            f"引用的标准证书 {parent_no} 结论为不合格，不得作为校准依据",
        )
    covers = conn.execute(
        "SELECT tstzrange(valid_from, valid_to, '[)') @> %s AS ok FROM certificates "
        "WHERE cert_no = %s",
        (calibrated_at, parent_no),
    ).fetchone()
    if not covers or not covers["ok"]:
        raise ApiError(
            422,
            "reference_out_of_validity",
            f"校准时刻 {calibrated_at.isoformat()} 不在标准证书 {parent_no} 的"
            f"有效期 [{parent['valid_from'].isoformat()}, {parent['valid_to'].isoformat()}) 内",
        )
    return parent


def _assert_no_cycle(conn, parent_no: str, new_cert_no: str) -> None:
    """防御性成环检测：若新证书编号已出现在上游链中，则拒绝（正常情况下不会触发）。"""
    sql = """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no
            FROM certificates c JOIN up u ON c.cert_no = u.parent_cert_no
        )
        SELECT 1 FROM up WHERE parent_cert_no = %s LIMIT 1
    """
    if conn.execute(sql, (parent_no, new_cert_no)).fetchone():
        raise ApiError(
            422,
            "reference_cycle",
            f"引用关系将形成环路（{new_cert_no} 已在 {parent_no} 的上游链中）",
        )


def _assert_no_overlap(conn, device_id: str, valid_from, valid_to, exclude_no: str | None) -> None:
    row = conn.execute(
        """
        SELECT cert_no FROM certificates
        WHERE device_id = %s AND status = 'ACTIVE' AND cert_no IS DISTINCT FROM %s
          AND tstzrange(valid_from, valid_to, '[)') && tstzrange(%s, %s, '[)')
        LIMIT 1
        """,
        (device_id, exclude_no, valid_from, valid_to),
    ).fetchone()
    if row:
        raise ApiError(
            422,
            "validity_overlap",
            f"设备 {device_id} 已生效证书 {row['cert_no']} 的有效期与本次签发区间重叠"
            "（区间按半开 [valid_from, valid_to) 判定）",
            details={"conflict_cert_no": row["cert_no"]},
        )


def _insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req,
                        issue_batch_key: str | None = None) -> dict:
    try:
        conn.execute(
            """
            INSERT INTO certificates (
                cert_no, kind, device_id, device_name, calibrated_at,
                valid_from, valid_to, conclusion, parent_cert_no, operator, remark,
                issue_batch_key
            ) VALUES (
                %s, %s::cert_kind, %s, %s, %s, %s, %s, %s::cert_conclusion, %s, %s, %s, %s
            )
            """,
            (
                req.cert_no, kind, req.device_id, req.device_name, req.calibrated_at,
                req.valid_from, req.valid_to, conclusion, parent_no, req.operator, req.remark,
                issue_batch_key,
            ),
        )
    except psycopg.errors.UniqueViolation:
        raise ApiError(409, "cert_duplicate", f"证书编号已存在: {req.cert_no}")
    except psycopg.errors.ExclusionViolation:
        # 并发事务抢先插入了重叠区间——排他约束兜底
        raise ApiError(
            409,
            "validity_overlap_concurrent",
            f"设备 {req.device_id} 已存在与本次有效期重叠的生效证书（并发签发冲突），"
            f"证书编号 {req.cert_no} 未签发",
        )
    except psycopg.errors.ForeignKeyViolation:
        raise ApiError(422, "invalid_reference", f"引用的标准证书不存在: {parent_no}")
    except psycopg.errors.CheckViolation as exc:
        raise ApiError(422, "rule_violation", f"签发被数据库完整性规则拒绝: {exc}")
    return get_certificate(conn, req.cert_no)


def issue_certificate(conn, req) -> dict:
    """签发普通校准证书。"""
    with conn.transaction():
        _validate_parent(conn, req.parent_cert_no, req.calibrated_at)
        _assert_no_cycle(conn, req.parent_cert_no, req.cert_no)
        _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
        cert = _insert_certificate(
            conn, kind="CALIBRATION", conclusion=req.conclusion,
            parent_no=req.parent_cert_no, req=req,
        )
    chain = get_chain(conn, req.cert_no)
    return {"certificate": cert, "chain": chain}


def issue_reference(conn, req) -> dict:
    """登记基准设备合格证书（追溯起点，恒为合格、无上游）。"""
    with conn.transaction():
        _assert_no_overlap(conn, req.device_id, req.valid_from, req.valid_to, req.cert_no)
        cert = _insert_certificate(
            conn, kind="REFERENCE", conclusion="PASS", parent_no=None, req=req,
        )
    chain = get_chain(conn, req.cert_no)
    return {"certificate": cert, "chain": chain}


# ---------------------------------------------------------------------------
# 交接批次：一组离线校准记录整批签发（全有或全无，批次键幂等）
# ---------------------------------------------------------------------------

def _jsonable(value):
    """递归把行内 datetime 转为 ISO 字符串，供 JSONB 序列化。"""
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _canonical_item(item) -> dict:
    """单项的规范化表示；缺失字段以 None 固定，保证同语义请求指纹相同。"""
    return {
        "cert_no": item.cert_no,
        "kind": item.kind,
        "device_id": item.device_id,
        "device_name": item.device_name,
        "calibrated_at": item.calibrated_at.isoformat(),
        "valid_from": item.valid_from.isoformat(),
        "valid_to": item.valid_to.isoformat(),
        # 基准证书结论恒 PASS：无论调用方传 null 还是 PASS，指纹一致
        "conclusion": "PASS" if item.kind == "REFERENCE" else item.conclusion,
        "parent_cert_no": None if item.kind == "REFERENCE" else item.parent_cert_no,
        "operator": item.operator,
        "remark": item.remark,
    }


def _request_fingerprint(items) -> tuple[str, list[dict]]:
    """与列表顺序无关的请求指纹：按 cert_no 排序后做规范化 JSON + SHA-256。"""
    canon = sorted((_canonical_item(i) for i in items), key=lambda d: d["cert_no"])
    blob = json.dumps(canon, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest(), canon


class _BatchValidation(Exception):
    """批次预检收集到的错误集合；任一记录不合规即整批拒绝。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors


def _batch_error(index: int, cert_no: str | None, code: str, message: str,
                 details: dict | None = None) -> dict:
    err = {"index": index, "cert_no": cert_no, "code": code, "message": message}
    if details:
        err["details"] = details
    return err


def _validate_batch_items(conn, items) -> dict[str, object]:
    """在单事务内对整批做全部签发前校验，返回 {cert_no: item}。

    校验项（复用单张签发的错误码与语义）：
      1. kind 与 conclusion/parent_cert_no 组合合法；
      2. 批内 cert_no 唯一，且与库内现有证书不冲突；
      3. 引用：可指向库内或本批证书；库内上游须 ACTIVE/PASS 且校准时刻在有效期；
      4. 引用无环（以库内链为根、本批为新增边做图论检测）；
      5. 同设备 ACTIVE 有效期不重叠（与库内及批内其他记录都不重叠）。
    """
    errors: list[dict] = []
    by_no: dict[str, object] = {}

    # --- 1. 字段组合 + 批内编号唯一 -----------------------------------------
    for idx, it in enumerate(items):
        if it.kind == "REFERENCE":
            if it.parent_cert_no is not None:
                errors.append(_batch_error(
                    idx, it.cert_no, "reference_with_parent",
                    f"基准证书 {it.cert_no} 不得引用上游证书（parent_cert_no 必须为空）",
                ))
            if it.conclusion not in (None, "PASS"):
                errors.append(_batch_error(
                    idx, it.cert_no, "reference_not_qualified",
                    f"基准证书 {it.cert_no} 结论必须为合格（PASS）",
                ))
        else:
            if not it.parent_cert_no:
                errors.append(_batch_error(
                    idx, it.cert_no, "invalid_reference",
                    f"普通证书 {it.cert_no} 必须引用一张标准设备证书（parent_cert_no 不能为空）",
                ))
            if not it.conclusion:
                errors.append(_batch_error(
                    idx, it.cert_no, "conclusion_required",
                    f"普通证书 {it.cert_no} 必须给出校准结论 conclusion(PASS/FAIL)",
                ))
        if it.cert_no in by_no:
            errors.append(_batch_error(
                idx, it.cert_no, "batch_cert_no_duplicate",
                f"证书编号 {it.cert_no} 在本批次内重复出现（批内编号必须唯一）",
            ))
        else:
            by_no[it.cert_no] = it

    # --- 2. 与库内现有证书的编号冲突（一次性查询） ---------------------------
    existing = {
        r["cert_no"]: r
        for r in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (list(by_no.keys()),),
        ).fetchall()
    }
    for it in items:
        if it.cert_no in existing:
            errors.append(_batch_error(
                next(i for i, x in enumerate(items) if x is it),
                it.cert_no, "cert_duplicate",
                f"证书编号已存在: {it.cert_no}",
            ))

    # --- 3. 引用解析与有效性 -------------------------------------------------
    # 对“库内被引用证书”按编号排序加 FOR KEY SHARE 行锁：
    #   - 与撤销事务的 FOR UPDATE 互斥，确保校验通过后到本批提交前，
    #     被引用上游不可能被撤销（与外键插入所用锁同一级别）；
    #   - 多个批次引用同一上游时互不阻塞（KEY SHARE 彼此兼容），不无故串行；
    #   - 固定加锁顺序（按 cert_no）避免与撤销路径形成锁环；万一中环由 PG
    #     死锁检测裁决，映射为 409 issue_batch_concurrent，整批可重试。
    db_parent_nos = sorted({
        it.parent_cert_no
        for it in items
        if it.kind == "CALIBRATION" and it.parent_cert_no and it.parent_cert_no not in by_no
    })
    db_parents: dict[str, dict] = {}
    if db_parent_nos:
        rows = conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s) "
            f"ORDER BY cert_no FOR KEY SHARE",
            (db_parent_nos,),
        ).fetchall()
        db_parents = {r["cert_no"]: r for r in rows}

    for idx, it in enumerate(items):
        if it.kind != "CALIBRATION" or not it.parent_cert_no:
            continue
        pno = it.parent_cert_no
        parent = db_parents.get(pno)
        if pno in by_no:
            # 批内引用：写入时本批记录必然是 ACTIVE 新证；合格性看批内结论，
            # 并同样要求校准时刻落在批内上游的有效期内。
            p_item = by_no[pno]
            if p_item.kind == "CALIBRATION" and p_item.conclusion != "PASS":
                errors.append(_batch_error(
                    idx, it.cert_no, "reference_not_qualified",
                    f"引用的批内证书 {pno} 结论为不合格，不得作为校准依据",
                ))
            if not (p_item.valid_from <= it.calibrated_at < p_item.valid_to):
                errors.append(_batch_error(
                    idx, it.cert_no, "reference_out_of_validity",
                    f"校准时刻 {it.calibrated_at.isoformat()} 不在批内证书 {pno} 的"
                    f"有效期 [{p_item.valid_from.isoformat()}, "
                    f"{p_item.valid_to.isoformat()}) 内",
                ))
            continue
        if parent is None:
            errors.append(_batch_error(
                idx, it.cert_no, "invalid_reference",
                f"引用的标准证书不存在: {pno}",
            ))
            continue
        if parent["status"] != "ACTIVE":
            errors.append(_batch_error(
                idx, it.cert_no, "reference_not_active",
                f"引用的标准证书 {pno} 当前状态为 {parent['status']}，"
                "已撤销/失效证书不得作为校准依据",
            ))
            continue
        if parent["conclusion"] != "PASS":
            errors.append(_batch_error(
                idx, it.cert_no, "reference_not_qualified",
                f"引用的标准证书 {pno} 结论为不合格，不得作为校准依据",
            ))
            continue
        if not (parent["valid_from"] <= it.calibrated_at < parent["valid_to"]):
            errors.append(_batch_error(
                idx, it.cert_no, "reference_out_of_validity",
                f"校准时刻 {it.calibrated_at.isoformat()} 不在标准证书 {pno} 的"
                f"有效期 [{parent['valid_from'].isoformat()}, "
                f"{parent['valid_to'].isoformat()}) 内",
            ))

    # --- 4. 引用无环（仅本批新增边可能引入环；库内边不可能指向尚未入库的本批节点） ---
    # 迭代式三色 DFS：重入 GRAY（当前递归栈）节点即发现环，环=栈上从该节点起的节点。
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {no: WHITE for no in by_no}
    cyclic: set[str] = set()

    def parents_of(no: str):
        it = by_no[no]
        return [it.parent_cert_no] if it.kind == "CALIBRATION" and it.parent_cert_no else []

    for start in list(by_no.keys()):
        if color[start] != WHITE:
            continue
        color[start] = GRAY
        stack = [(start, 0)]  # (节点, 下一个待访问父边下标)
        while stack:
            node, pi = stack[-1]
            ps = parents_of(node)
            if pi >= len(ps):
                color[node] = BLACK
                stack.pop()
                continue
            stack[-1] = (node, pi + 1)
            pno = ps[pi]
            if pno not in by_no:
                continue  # 库内引用：该方向安全终止
            if color[pno] == GRAY:
                depth = next(d for d, (n, _) in enumerate(stack) if n == pno)
                cyclic.update(n for n, _ in stack[depth:])
            elif color[pno] == WHITE:
                color[pno] = GRAY
                stack.append((pno, 0))

    reported_cycles: set[str] = set()
    for idx, it in enumerate(items):
        if it.cert_no in cyclic and it.cert_no not in reported_cycles:
            reported_cycles.add(it.cert_no)
            errors.append(_batch_error(
                idx, it.cert_no, "reference_cycle",
                f"批内引用关系将形成环路（证书 {it.cert_no} 位于环上）",
            ))

    # --- 5. 有效期不重叠 -----------------------------------------------------
    # 5a. 与库内 ACTIVE 证书：按本批涉及设备一次性取出冲突集。
    device_ids = sorted({it.device_id for it in items})
    db_active = conn.execute(
        """
        SELECT cert_no, device_id, valid_from, valid_to
        FROM certificates
        WHERE status = 'ACTIVE' AND device_id = ANY(%s)
        """,
        (device_ids,),
    ).fetchall()
    for idx, it in enumerate(items):
        for row in db_active:
            if row["device_id"] == it.device_id and row["cert_no"] not in by_no \
                    and it.valid_from < row["valid_to"] and row["valid_from"] < it.valid_to:
                errors.append(_batch_error(
                    idx, it.cert_no, "validity_overlap",
                    f"设备 {it.device_id} 已生效证书 {row['cert_no']} 的有效期与"
                    f"本批证书 {it.cert_no} 重叠（区间按半开 [valid_from, valid_to) 判定）",
                    details={"conflict_cert_no": row["cert_no"]},
                ))
                break

    # 5b. 批内同设备记录互相不重叠（全对比较，保证每个冲突编号都被定位）。
    by_device: dict[str, list] = {}
    for it in items:
        by_device.setdefault(it.device_id, []).append(it)
    intra_reported: set[tuple[str, str]] = set()
    for dev, group in by_device.items():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.cert_no == b.cert_no:
                    continue  # 批内重复编号已在第 1 步报错
                if a.valid_from < b.valid_to and b.valid_from < a.valid_to:
                    for x, y in ((a, b), (b, a)):
                        key = (x.cert_no, y.cert_no)
                        if key not in intra_reported:
                            intra_reported.add(key)
                            errors.append(_batch_error(
                                next(k for k, t in enumerate(items) if t is x),
                                x.cert_no, "validity_overlap",
                                f"设备 {dev} 的本批证书 {x.cert_no} 与 {y.cert_no} 有效期重叠"
                                "（区间按半开 [valid_from, valid_to) 判定）",
                                details={"conflict_cert_no": y.cert_no},
                            ))

    if errors:
        errors.sort(key=lambda e: (e["index"], e["code"]))
        raise _BatchValidation(errors)
    return {"by_no": by_no}


def _topo_order(items, by_no: dict) -> list:
    """按引用关系给出写入顺序（父先于子）；调用前已保证无环。"""
    order: list = []
    placed: set[str] = set()
    remaining = {it.cert_no: it for it in items}

    def place(no: str):
        if no in placed or no not in remaining:
            return
        it = remaining[no]
        pno = it.parent_cert_no
        if it.kind == "CALIBRATION" and pno in remaining:
            place(pno)
        order.append(it)
        placed.add(no)

    for it in sorted(items, key=lambda x: x.cert_no):
        place(it.cert_no)
    return order


def issue_batch(conn, req) -> tuple[dict, bool]:
    """整批签发。返回 (响应体, 是否同键重放)。

    并发与幂等：
      - 进入事务先以 INSERT ... ON CONFLICT DO NOTHING 抢占批次键：
        抢到 → 持该行事务锁做全部校验与写入（并发同键提交被阻塞到本事务结束）；
        没抢到 → 前一个事务必定已落定：同指纹返回首次结果，异指纹 409。
      - 校验失败整批 ROLLBACK，占位行随之消失，批次键可被原请求重试。
      - 成功后结果快照固化在 issue_batches.result_snapshot，重放原样返回，
        即使证书随后被撤销。
    """
    items = req.certificates
    request_hash, canonical = _request_fingerprint(items)
    request_body = {"certificates": canonical}

    try:
        return _issue_batch_locked(conn, req, items, request_hash, request_body)
    except psycopg.errors.DeadlockDetected:
        # 与并发撤销/签发形成锁环，PG 已中止本事务：整批未落地，客户端可安全重试
        raise ApiError(
            409, "issue_batch_concurrent",
            f"批次 {req.batch_key} 与并发的单张签发/撤销发生锁冲突，整批未签发，可重试",
        )


def _issue_batch_locked(conn, req, items, request_hash, request_body) -> tuple[dict, bool]:
    with conn.transaction():
        claimed = conn.execute(
            """
            INSERT INTO issue_batches (batch_key, request_hash, request_body)
            VALUES (%s, %s, %s::jsonb)
            ON CONFLICT (batch_key) DO NOTHING
            RETURNING batch_key
            """,
            (req.batch_key, request_hash, json.dumps(request_body, ensure_ascii=False)),
        ).fetchone()

        if claimed is None:
            # 同键的前一个事务已落定（本 INSERT 会等其行锁释放后才判定冲突）
            prev = conn.execute(
                "SELECT request_hash, request_body, result_snapshot FROM issue_batches "
                "WHERE batch_key = %s",
                (req.batch_key,),
            ).fetchone()
            if prev["request_hash"] != request_hash:
                raise ApiError(
                    409, "issue_batch_conflict",
                    f"批次键 {req.batch_key} 已被另一个不同请求成功签发"
                    "（同键不同请求冲突）；如需重放请发送与首次完全一致的请求",
                    details={"batch_key": req.batch_key, "first_request": prev["request_body"]},
                )
            snapshot = prev["result_snapshot"]
            if snapshot is None:
                # 理论不可达：前一事务若失败会回滚掉占位行；此处防御
                raise ApiError(409, "issue_batch_state",
                               f"批次 {req.batch_key} 正由另一请求处理中")
            # 快照固化首次结果（replayed=False 为历史原貌）；本次返回标注为重放
            replay = json.loads(json.dumps(snapshot))
            replay["replayed"] = True
            return replay, True

        # ---- 本事务持有批次键：执行全部签发前校验 --------------------------
        try:
            validated = _validate_batch_items(conn, items)
        except _BatchValidation as exc:
            raise ApiError(
                422, "batch_validation_failed",
                f"批次 {req.batch_key} 中有 {len(exc.errors)} 项记录不合规，整批不予签发"
                "（任何记录未入库）",
                details={"batch_key": req.batch_key, "errors": exc.errors},
            )

        # ---- 按引用拓扑序写入（父先于子，满足外键） ------------------------
        for it in _topo_order(items, validated["by_no"]):
            if it.kind == "REFERENCE":
                kind, conclusion, parent_no = "REFERENCE", "PASS", None
            else:
                kind, conclusion, parent_no = "CALIBRATION", it.conclusion, it.parent_cert_no
            try:
                _insert_certificate(
                    conn, kind=kind, conclusion=conclusion,
                    parent_no=parent_no, req=it, issue_batch_key=req.batch_key,
                )
            except ApiError:
                # 竞态穿透（并发单张签发/撤销在预检后改库）：整批回滚并映射为并发冲突
                raise ApiError(
                    409, "issue_batch_concurrent",
                    f"批次 {req.batch_key} 与并发的单张签发/撤销冲突，整批未签发，可重试",
                )

        # ---- 组装从基准到每张证书的追溯链（与请求列表同序） ----------------
        results = [
            {"cert_no": it.cert_no,
             "certificate": get_certificate(conn, it.cert_no),
             "chain": get_chain(conn, it.cert_no)}
            for it in items
        ]
        issued_at = conn.execute("SELECT now() AS now").fetchone()["now"]
        response = {
            "batch_key": req.batch_key,
            "issued_at": issued_at,
            "replayed": False,
            "certificates": results,
            "cert_count": len(results),
        }
        # 固化首次结果（触发器保证此后不可改）
        conn.execute(
            "UPDATE issue_batches SET result_snapshot = %s::jsonb, issued_at = %s "
            "WHERE batch_key = %s",
            (json.dumps(_jsonable(response), ensure_ascii=False), issued_at, req.batch_key),
        )
        return response, False


def get_issue_batch(conn, batch_key: str) -> dict:
    """查询交接批次：返回批次元信息及各证书的**当前**状态与追溯链。"""
    head = conn.execute(
        "SELECT batch_key, created_at, issued_at, result_snapshot FROM issue_batches "
        "WHERE batch_key = %s",
        (batch_key,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "issue_batch_not_found", f"交接批次不存在: {batch_key}")
    if head["result_snapshot"] is None:
        raise ApiError(409, "issue_batch_incomplete", f"批次 {batch_key} 尚未完成签发")

    snapshot = head["result_snapshot"]
    certs_live = []
    for first in snapshot["certificates"]:
        no = first["cert_no"]
        certs_live.append({
            "cert_no": no,
            "certificate": get_certificate(conn, no),
            "chain": get_chain(conn, no),
        })
    return {
        "batch_key": head["batch_key"],
        "created_at": head["created_at"],
        "issued_at": head["issued_at"],
        "cert_count": len(certs_live),
        "certificates": certs_live,
    }


def list_issue_batches(conn, limit: int, offset: int) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM issue_batches WHERE result_snapshot IS NOT NULL").fetchone()["n"]
    rows = conn.execute(
        """
        SELECT batch_key, created_at, issued_at,
               jsonb_array_length(result_snapshot->'certificates') AS cert_count
        FROM issue_batches
        WHERE result_snapshot IS NOT NULL
        ORDER BY issued_at DESC, batch_key ASC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return {"items": rows, "total": total, "limit": limit, "offset": offset}


# ---------------------------------------------------------------------------
# 撤销与级联失效
# ---------------------------------------------------------------------------

def _gather_active_descendants(conn, root_no: str) -> list[dict]:
    """找出所有状态仍为 ACTIVE 的下游证书。

    path 为“相对被撤销证书”的路径（不含 root 本身），
    完整追溯链在写入快照时与 root 自身的上游链拼接得到。
    """
    sql = """
        WITH RECURSIVE d AS (
            SELECT cert_no, 1 AS depth, ARRAY[cert_no] AS path
            FROM certificates
            WHERE parent_cert_no = %s AND status = 'ACTIVE'
            UNION ALL
            SELECT c.cert_no, d.depth + 1, d.path || c.cert_no
            FROM certificates c JOIN d ON c.parent_cert_no = d.cert_no
            WHERE c.status = 'ACTIVE'
        )
        CYCLE cert_no SET is_cycle USING cycle_path
        SELECT cert_no, depth, path FROM d
        WHERE NOT is_cycle
    """
    return conn.execute(sql, (root_no,)).fetchall()


def _root_prefix_chain(conn, root_no: str) -> list[str]:
    """被撤销证书自身的链：从追溯起点（基准）到该证书，含两端。"""
    rows = conn.execute(
        """
        WITH RECURSIVE up AS (
            SELECT cert_no, parent_cert_no, 0 AS depth
            FROM certificates WHERE cert_no = %s
            UNION ALL
            SELECT c.cert_no, c.parent_cert_no, up.depth - 1
            FROM certificates c JOIN up ON c.cert_no = up.parent_cert_no
        )
        SELECT cert_no FROM up ORDER BY depth ASC
        """,
        (root_no,),
    ).fetchall()
    return [r["cert_no"] for r in rows]


def revoke_certificate(conn, cert_no: str, reason: str, operator: str | None) -> dict:
    """主动撤销一张生效证书，并级联失效其全部下游证书。

    并发正确性：
      1. 先对被撤销证书加 FOR UPDATE 行锁；新插入的下游证书在外键检查时需获取
         该行的 FOR KEY SHARE 锁，会被阻塞到本事务提交之后；
      2. 递归收集下游并按 cert_no 排序加行锁后再次收集，直到集合不再扩大，
         覆盖“撤销进行中恰有下游证书提交”的竞态；
      3. 所有 UPDATE/INSERT 在同一事务中提交。
    """
    with conn.transaction():
        root = _get_cert_row(conn, cert_no, lock=True)
        if root is None:
            raise ApiError(404, "cert_not_found", f"证书不存在: {cert_no}")
        if root["status"] != "ACTIVE":
            raise ApiError(
                409,
                "cert_not_active",
                f"证书 {cert_no} 当前状态为 {root['status']}，"
                "仅生效中的证书可被撤销（已失效证书不能重复撤销）",
            )

        locked: set[str] = set()
        descendants: list[dict] = []
        # 循环直到“收集结果”与“已锁定集合”一致，保证不会漏掉新提交的下游
        while True:
            rows = _gather_active_descendants(conn, cert_no)
            fresh = [r for r in rows if r["cert_no"] not in locked]
            if not fresh:
                descendants = rows
                break
            nos = sorted(r["cert_no"] for r in fresh)
            conn.execute(
                "SELECT cert_no FROM certificates "
                "WHERE cert_no = ANY(%s) ORDER BY cert_no FOR UPDATE",
                (nos,),
            )
            locked.update(nos)

        batch = conn.execute(
            "INSERT INTO revocations (cert_no, reason, operator) "
            "VALUES (%s, %s, %s) RETURNING batch_id, revoked_at",
            (cert_no, reason, operator),
        ).fetchone()

        # 被撤销证书本身
        conn.execute(
            """
            UPDATE certificates
            SET status = 'REVOKED', revoked_reason = %s, revoked_at = %s
            WHERE cert_no = %s
            """,
            (reason, batch["revoked_at"], cert_no),
        )

        if descendants:
            # 完整引用链快照：追溯起点 -> 被撤销证书 -> 下游证书
            prefix = _root_prefix_chain(conn, cert_no)

            # 全部下游证书级联失效
            conn.execute(
                """
                UPDATE certificates AS c SET
                    status = 'INVALID',
                    revoked_reason = %s,
                    revoked_at = %s
                FROM unnest(%s::text[]) AS d(cert_no)
                WHERE c.cert_no = d.cert_no
                """,
                (
                    f"上游证书 {cert_no} 已撤销，引用链级联失效",
                    batch["revoked_at"],
                    [r["cert_no"] for r in descendants],
                ),
            )
            # 保存不可改写的失效明细与引用链快照
            full_chains = [(prefix, r) for r in descendants]
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO invalidations (batch_id, cert_no, chain, position)
                    VALUES (%s, %s, %s, %s)
                    """,
                    [
                        (
                            batch["batch_id"],
                            r["cert_no"],
                            [*prefix, *r["path"]],
                            len(prefix) + len(r["path"]),
                        )
                        for prefix, r in full_chains
                    ],
                )

    # 事务提交后组装返回内容
    revoked = get_certificate(conn, cert_no)
    affected = _build_invalidated_items(
        conn, batch["batch_id"], batch["revoked_at"], cert_no
    )
    return {
        "batch_id": batch["batch_id"],
        "revoked": revoked,
        "reason": reason,
        "operator": operator,
        "revoked_at": batch["revoked_at"],
        "affected_count": len(affected),
        "affected": affected,
    }


def _build_invalidated_items(conn, batch_id, revoked_at, root_no: str) -> list[dict]:
    rows = conn.execute(
        "SELECT cert_no, chain FROM invalidations WHERE batch_id = %s "
        "ORDER BY position, cert_no",
        (batch_id,),
    ).fetchall()
    if not rows:
        return []
    all_nos = {no for r in rows for no in r["chain"]}
    cert_map = {
        c["cert_no"]: c
        for c in conn.execute(
            f"SELECT {_VIEW_COLS} FROM v_certificates WHERE cert_no = ANY(%s)",
            (list(all_nos),),
        ).fetchall()
    }
    items = []
    for r in rows:
        chain = [cert_map[no] for no in r["chain"] if no in cert_map]
        items.append(
            {
                "cert_no": r["cert_no"],
                "chain": chain,
                # 深度按链上相对被撤销证书的层数计
                "depth": len(r["chain"]) - 1,
            }
        )
    return items


def get_revocation(conn, batch_id: int) -> dict:
    head = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r WHERE r.batch_id = %s
        """,
        (batch_id,),
    ).fetchone()
    if head is None:
        raise ApiError(404, "revocation_not_found", f"撤销批次不存在: {batch_id}")
    cert_no = head.pop("cert_no")
    return {
        **head,
        "revoked": get_certificate(conn, cert_no),
        "affected": _build_invalidated_items(conn, batch_id, head["revoked_at"], cert_no),
    }


def list_revocations(conn, limit: int, offset: int) -> dict:
    total = conn.execute("SELECT count(*) AS n FROM revocations").fetchone()["n"]
    items = conn.execute(
        """
        SELECT r.batch_id, r.cert_no, r.reason, r.operator, r.revoked_at,
               (SELECT count(*) FROM invalidations i WHERE i.batch_id = r.batch_id) AS affected_count
        FROM revocations r
        ORDER BY r.batch_id DESC
        LIMIT %s OFFSET %s
        """,
        (limit, offset),
    ).fetchall()
    return {"items": items, "total": total, "limit": limit, "offset": offset}
