"""Pydantic 请求/响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field


class SyncRequest(BaseModel):
    list_url: str = Field(..., description="公共采购网站公告列表页 URL")


class AnnouncementItem(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None
    source_url: Optional[str] = None
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None


class Chain(BaseModel):
    chain_id: str = Field(..., description="链标识，即首次公告编号")
    project_no: Optional[str] = None
    status: str = Field(..., description="ok / conflict / cancelled")
    effective_deadline: Optional[str] = Field(
        None, description="有效截止时间；冲突或已撤销时为 null"
    )
    announcements: list[AnnouncementItem] = []


class OrphanAnnouncement(BaseModel):
    notice_no: str
    project_no: Optional[str] = None
    notice_type: Optional[str] = None
    status: str
    conflict_reasons: list[str] = []
    effective_deadline: Optional[str] = None
    source_url: Optional[str] = None
    publish_time: Optional[str] = None
    deadline: Optional[str] = None
    ref_notice_no: Optional[str] = None


class QueryResponse(BaseModel):
    chains: list[Chain] = []
    orphans: list[OrphanAnnouncement] = []


class SyncResponse(BaseModel):
    run_id: str
    status: str
    total_listed: int
    fetched_ok: int
    failed_count: int
    kept_last: int
    message: Optional[str] = None


class ManifestEntry(BaseModel):
    """核验时观察到的一个附件清单条目。"""
    url: str
    filename: str
    declared_sha256: Optional[str] = None


class ManifestChangeEntry(BaseModel):
    url: str
    filename: str
    declared_sha256: Optional[str] = None


class ManifestChange(BaseModel):
    """核验前后两次读取详情页观察到的清单差异。"""
    added: list[ManifestChangeEntry] = []
    removed: list[ManifestChangeEntry] = []
    changed: list[ManifestChangeEntry] = Field(
        default=[], description="同一链接的文件名或声明摘要发生变化"
    )


class AttachmentCheckItem(BaseModel):
    filename: str = Field(..., description="附件文件名（同名不同链接分别核验）")
    url: str = Field(..., description="附件来源链接")
    declared_sha256: Optional[str] = Field(
        None, description="详情页声明的 SHA-256；缺失时为 null"
    )
    actual_sha256: Optional[str] = Field(
        None, description="按下载字节实测的 SHA-256；下载失败时为 null"
    )
    status: str = Field(
        ..., description="ok / digest_mismatch / missing_declared_digest / "
        "download_failed"
    )
    reason: Optional[str] = Field(None, description="异常原因说明；正常时为 null")


class VerificationResponse(BaseModel):
    notice_no: str
    source_url: Optional[str] = None
    verification_id: str
    status: str = Field(
        ..., description="all_available / needs_review；任何异常都不得报全部可用"
    )
    total_attachments: int
    available_count: int = Field(..., description="状态为 ok 的附件数量")
    reasons: list[str] = Field(
        default=[], description="整次核验的异常原因代码汇总"
    )
    manifest_changed: bool
    manifest_change: ManifestChange
    attachments: list[AttachmentCheckItem] = []
    pre_snapshot: list[ManifestEntry] = Field(
        default=[], description="核验前读取到的附件清单"
    )
    post_snapshot: list[ManifestEntry] = Field(
        default=[], description="核验后读取到的附件清单"
    )
    checked_at: str
