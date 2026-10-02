"""HTTP API 路由。

约定：
- POST   /api/v1/reference-certificates        登记基准设备合格证书（追溯起点）
- POST   /api/v1/certificates                  签发普通校准证书
- GET    /api/v1/certificates                  证书列表（可按设备/状态/类型过滤）
- GET    /api/v1/certificates/{cert_no}        证书详情（历史内容始终可查）
- GET    /api/v1/certificates/{cert_no}/chain  完整追溯引用链
- GET    /api/v1/certificates/{cert_no}/downstream  全部下游证书（影响面预览）
- POST   /api/v1/certificates/{cert_no}/revoke 撤销证书并级联失效下游
- GET    /api/v1/devices/{device_id}/certificates  某设备全部证书（含历史）
- GET    /api/v1/revocations                   撤销批次列表
- GET    /api/v1/revocations/{batch_id}        撤销批次详情（受影响证书及引用链）
"""
from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from psycopg_pool import ConnectionPool

from . import repository as repo
from .schemas import (
    CertificateIssueRequest,
    ReferenceIssueRequest,
    RevokeRequest,
)

router = APIRouter(prefix="/api/v1")


def pool_dep(request: Request) -> ConnectionPool:
    return request.app.state.pool


@router.post("/reference-certificates", status_code=201)
def issue_reference(req: ReferenceIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_reference(conn, req)


@router.post("/certificates", status_code=201)
def issue_certificate(req: CertificateIssueRequest, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.issue_certificate(conn, req)


@router.get("/certificates")
def list_certificates(
    device_id: str | None = None,
    status: Literal["ACTIVE", "REVOKED", "INVALID"] | None = None,
    kind: Literal["REFERENCE", "CALIBRATION"] | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_certificates(conn, device_id, status, kind, limit, offset)


@router.get("/certificates/{cert_no}")
def get_certificate(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_certificate(conn, cert_no)


@router.get("/certificates/{cert_no}/chain")
def get_chain(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_chain(conn, cert_no)


@router.get("/certificates/{cert_no}/downstream")
def get_downstream(cert_no: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_downstream(conn, cert_no)


@router.post("/certificates/{cert_no}/revoke", status_code=200)
def revoke_certificate(
    cert_no: str, req: RevokeRequest, pool: ConnectionPool = Depends(pool_dep)
):
    with pool.connection() as conn:
        return repo.revoke_certificate(conn, cert_no, req.reason, req.operator)


@router.get("/devices/{device_id}/certificates")
def device_history(device_id: str, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_device_history(conn, device_id)


@router.get("/revocations")
def list_revocations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    pool: ConnectionPool = Depends(pool_dep),
):
    with pool.connection() as conn:
        return repo.list_revocations(conn, limit, offset)


@router.get("/revocations/{batch_id}")
def get_revocation(batch_id: int, pool: ConnectionPool = Depends(pool_dep)):
    with pool.connection() as conn:
        return repo.get_revocation(conn, batch_id)
