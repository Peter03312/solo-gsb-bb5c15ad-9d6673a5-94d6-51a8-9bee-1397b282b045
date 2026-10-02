"""Pydantic 请求与响应模型。

时间一律使用带时区的 ISO 8601（如 2026-03-01T09:00:00+08:00），
不传时区时按 UTC 处理。
有效期统一按半开区间 [valid_from, valid_to) 解释，首尾相接不算重叠。
"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CertKind = Literal["REFERENCE", "CALIBRATION"]
CertStatus = Literal["ACTIVE", "REVOKED", "INVALID"]
CertConclusion = Literal["PASS", "FAIL"]


class _IssueBase(BaseModel):
    cert_no: str = Field(..., min_length=1, max_length=128, description="证书编号，全局唯一")
    device_id: str = Field(..., min_length=1, max_length=128, description="被校/基准设备标识")
    device_name: str | None = Field(None, max_length=256, description="设备名称")
    calibrated_at: datetime | None = Field(
        None, description="校准时刻；缺省取 valid_from"
    )
    valid_from: datetime = Field(..., description="有效期开始")
    valid_to: datetime = Field(..., description="有效期结束（不含该时刻）")
    operator: str | None = Field(None, max_length=128)
    remark: str | None = Field(None, max_length=1024)

    @model_validator(mode="after")
    def _check_window(self):
        if self.valid_from >= self.valid_to:
            raise ValueError("valid_from 必须早于 valid_to")
        if self.calibrated_at is None:
            self.calibrated_at = self.valid_from
        if not (self.valid_from <= self.calibrated_at < self.valid_to):
            raise ValueError("calibrated_at 必须落在 [valid_from, valid_to) 内")
        return self


class ReferenceIssueRequest(_IssueBase):
    """登记基准（标准）设备合格证书——追溯起点，结论恒为合格。"""


class CertificateIssueRequest(_IssueBase):
    """签发普通校准证书。"""
    conclusion: CertConclusion = Field(..., description="校准结论 PASS/FAIL")
    parent_cert_no: str = Field(..., min_length=1, description="引用的标准设备证书编号")


class RevokeRequest(BaseModel):
    reason: str = Field(..., min_length=1, max_length=1024, description="撤销原因")
    operator: str | None = Field(None, max_length=128)


class CertificateOut(BaseModel):
    cert_no: str
    kind: CertKind
    device_id: str
    device_name: str | None = None
    calibrated_at: datetime
    valid_from: datetime
    valid_to: datetime
    conclusion: CertConclusion
    parent_cert_no: str | None = None
    operator: str | None = None
    remark: str | None = None
    status: CertStatus
    revoked_reason: str | None = None
    revoked_at: datetime | None = None
    created_at: datetime


class ChainNode(BaseModel):
    depth: int
    certificate: CertificateOut


class TraceChainOut(BaseModel):
    cert_no: str
    nodes: list[ChainNode] = Field(..., description="从追溯起点(基准)到目标证书的引用链")
    root_cert_no: str | None = None
    length: int


class IssueResponse(BaseModel):
    certificate: CertificateOut
    chain: TraceChainOut


class InvalidatedItem(BaseModel):
    cert_no: str
    chain: list[CertificateOut]
    depth: int = Field(..., description="相对于被撤销证书的下游层数，1 为直接引用者")


class RevocationOut(BaseModel):
    batch_id: int
    revoked: CertificateOut
    reason: str
    operator: str | None = None
    revoked_at: datetime
    affected_count: int
    affected: list[InvalidatedItem]


class DownstreamItem(BaseModel):
    cert_no: str
    depth: int
    status: CertStatus


class DownstreamOut(BaseModel):
    cert_no: str
    descendants: list[DownstreamItem]


class PagedCertificates(BaseModel):
    items: list[CertificateOut]
    total: int
    limit: int
    offset: int


class ErrorBody(BaseModel):
    error: dict
