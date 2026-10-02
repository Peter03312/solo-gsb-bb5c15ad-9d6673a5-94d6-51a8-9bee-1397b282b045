"""数据访问与核心业务规则（全部在数据库事务中执行）。"""
import psycopg

from .errors import ApiError

# v_certificates 视图的列顺序
_VIEW_COLS = (
    "cert_no, kind, device_id, device_name, calibrated_at, valid_from, valid_to, "
    "conclusion, parent_cert_no, operator, remark, status, revoked_reason, "
    "revoked_at, created_at"
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


def _insert_certificate(conn, *, kind: str, conclusion: str, parent_no: str | None, req) -> dict:
    try:
        conn.execute(
            """
            INSERT INTO certificates (
                cert_no, kind, device_id, device_name, calibrated_at,
                valid_from, valid_to, conclusion, parent_cert_no, operator, remark
            ) VALUES (
                %s, %s::cert_kind, %s, %s, %s, %s, %s, %s::cert_conclusion, %s, %s, %s
            )
            """,
            (
                req.cert_no, kind, req.device_id, req.device_name, req.calibrated_at,
                req.valid_from, req.valid_to, conclusion, parent_no, req.operator, req.remark,
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
