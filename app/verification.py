"""附件核验服务。

输入已同步公告的编号，重新读取其来源详情页（核验前后各一次），抽取附件
链接、文件名与站点声明的 SHA-256，逐项下载并按字节计算实测摘要：

- 核验前后清单（链接 / 文件名 / 声明摘要）发生任何变动 -> 整次 ``needs_review``；
- 详情页读取失败、附件下载失败、缺少声明摘要 -> 整次 ``needs_review``；
- 声明摘要与实测摘要不符 -> 整次 ``needs_review``，并指出对应文件；
- 只有全部附件逐项通过且清单无变动时才允许 ``all_available``。
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import datetime

from . import db, fetcher, parser
from .fetcher import FetchError
from .models import AttachmentCheckItem, ManifestChange, ManifestEntry

logger = logging.getLogger(__name__)

# 整次核验的异常原因代码
REASON_DETAIL_FETCH_FAILED = "detail_fetch_failed"
REASON_MANIFEST_CHANGED = "manifest_changed"
REASON_NO_ATTACHMENTS = "no_attachments_listed"
REASON_DIGEST_MISMATCH = "digest_mismatch"
REASON_MISSING_DECLARED_DIGEST = "missing_declared_digest"
REASON_DOWNLOAD_FAILED = "download_failed"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _entry(link: parser.AttachmentLink) -> dict:
    return {
        "url": link.url,
        "filename": link.filename,
        "declared_sha256": link.declared_sha256,
    }


def _snapshot(links: list[parser.AttachmentLink]) -> list[dict]:
    return [_entry(link) for link in links]


def diff_manifests(
    before: list[parser.AttachmentLink], after: list[parser.AttachmentLink]
) -> dict:
    """对比两次读取的附件清单，按链接识别新增、消失与条目变化。"""
    before_map = {link.url: link for link in before}
    after_map = {link.url: link for link in after}

    added = [_entry(after_map[u]) for u in after_map.keys() - before_map.keys()]
    removed = [_entry(before_map[u]) for u in before_map.keys() - after_map.keys()]
    changed: list[dict] = []
    for url in before_map.keys() & after_map.keys():
        old, new = before_map[url], after_map[url]
        if (
            old.filename != new.filename
            or old.declared_sha256 != new.declared_sha256
        ):
            changed.append(_entry(new))

    key = lambda item: item["url"]
    return {
        "added": sorted(added, key=key),
        "removed": sorted(removed, key=key),
        "changed": sorted(changed, key=key),
    }


def _check_one(link: parser.AttachmentLink) -> AttachmentCheckItem:
    """下载单个附件并按字节计算 SHA-256，返回逐项核验结果。"""
    if not link.declared_sha256:
        # 缺少声明摘要：仍尝试下载给出实测值，但该附件不能算可用
        try:
            content = fetcher.fetch_bytes(link.url)
            actual = hashlib.sha256(content).hexdigest()
        except FetchError as exc:
            logger.warning("附件下载失败 %s：%s", link.url, exc)
            return AttachmentCheckItem(
                filename=link.filename, url=link.url,
                declared_sha256=None, actual_sha256=None,
                status="download_failed", reason=str(exc),
            )
        return AttachmentCheckItem(
            filename=link.filename, url=link.url,
            declared_sha256=None, actual_sha256=actual,
            status="missing_declared_digest",
            reason="详情页未提供该附件的声明 SHA-256",
        )

    try:
        content = fetcher.fetch_bytes(link.url)
    except FetchError as exc:
        logger.warning("附件下载失败 %s：%s", link.url, exc)
        return AttachmentCheckItem(
            filename=link.filename, url=link.url,
            declared_sha256=link.declared_sha256, actual_sha256=None,
            status="download_failed", reason=str(exc),
        )

    actual = hashlib.sha256(content).hexdigest()
    if actual != link.declared_sha256.lower():
        return AttachmentCheckItem(
            filename=link.filename, url=link.url,
            declared_sha256=link.declared_sha256, actual_sha256=actual,
            status="digest_mismatch",
            reason=(
                f"文件 {link.filename} 声明摘要 {link.declared_sha256} "
                f"与实测摘要 {actual} 不符"
            ),
        )
    return AttachmentCheckItem(
        filename=link.filename, url=link.url,
        declared_sha256=link.declared_sha256, actual_sha256=actual,
        status="ok", reason=None,
    )


def verify_attachments(notice_no: str) -> dict:
    """对一条已同步公告执行附件核验，返回完整核验结果并持久化。"""
    conn = db.connect()
    db.init_db(conn)
    verification_id = uuid.uuid4().hex
    started_at = _now()
    row = conn.execute(
        "SELECT notice_no, source_url FROM announcements WHERE notice_no = ?",
        (notice_no,),
    ).fetchone()
    if row is None:
        conn.close()
        raise KeyError(notice_no)
    source_url = row["source_url"]

    reasons: list[str] = []
    pre_links: list[parser.AttachmentLink] = []
    post_links: list[parser.AttachmentLink] = []
    items: list[AttachmentCheckItem] = []
    change: dict = {"added": [], "removed": [], "changed": []}

    if not source_url:
        # 占位记录（详情从未抓取成功）没有来源链接，无法核验
        reasons.append(REASON_DETAIL_FETCH_FAILED)
        result = _build_payload(
            notice_no, source_url, verification_id, "needs_review", reasons,
            change, items, pre_links, post_links, started_at,
        )
        _persist(conn, result)
        conn.close()
        return result

    # 1) 核验前读取详情页
    try:
        pre_html = fetcher.fetch(source_url)
        pre_links = parser.parse_attachments(pre_html, source_url)
    except (FetchError, ValueError) as exc:
        logger.warning("核验前详情页读取失败 %s：%s", source_url, exc)
        reasons.append(REASON_DETAIL_FETCH_FAILED)
        result = _build_payload(
            notice_no, source_url, verification_id, "needs_review", reasons,
            change, items, pre_links, post_links, started_at,
        )
        _persist(conn, result)
        conn.close()
        return result

    if not pre_links:
        reasons.append(REASON_NO_ATTACHMENTS)

    # 2) 逐项下载并按字节核验（同名不同链接为不同条目，分别核验）
    for link in pre_links:
        item = _check_one(link)
        items.append(item)
        if item.status == "digest_mismatch":
            reasons.append(REASON_DIGEST_MISMATCH)
        elif item.status == "missing_declared_digest":
            reasons.append(REASON_MISSING_DECLARED_DIGEST)
        elif item.status == "download_failed":
            reasons.append(REASON_DOWNLOAD_FAILED)

    # 3) 核验后再次读取详情页并对比清单
    try:
        post_html = fetcher.fetch(source_url)
        post_links = parser.parse_attachments(post_html, source_url)
        change = diff_manifests(pre_links, post_links)
        if any(change.values()):
            reasons.append(REASON_MANIFEST_CHANGED)
    except (FetchError, ValueError) as exc:
        logger.warning("核验后详情页读取失败 %s：%s", source_url, exc)
        reasons.append(REASON_DETAIL_FETCH_FAILED)

    # 去重并保持稳定顺序
    reasons = sorted(set(reasons))
    available = sum(1 for item in items if item.status == "ok")
    status = "all_available" if not reasons and items else "needs_review"

    result = _build_payload(
        notice_no, source_url, verification_id, status, reasons,
        change, items, pre_links, post_links, started_at,
    )
    _persist(conn, result)
    conn.close()
    return result


def _build_payload(
    notice_no: str,
    source_url: str | None,
    verification_id: str,
    status: str,
    reasons: list[str],
    change: dict,
    items: list[AttachmentCheckItem],
    pre_links: list[parser.AttachmentLink],
    post_links: list[parser.AttachmentLink],
    started_at: str,
) -> dict:
    return {
        "notice_no": notice_no,
        "source_url": source_url,
        "verification_id": verification_id,
        "status": status,
        "total_attachments": len(pre_links),
        "available_count": sum(1 for item in items if item.status == "ok"),
        "reasons": reasons,
        "manifest_changed": any(change.values()),
        "manifest_change": ManifestChange(**change),
        "attachments": items,
        "pre_snapshot": [ManifestEntry(**_entry(link)) for link in pre_links],
        "post_snapshot": [ManifestEntry(**_entry(link)) for link in post_links],
        "checked_at": started_at,
    }


def _persist(conn, result: dict) -> None:
    conn.execute(
        "INSERT INTO verification_runs (verification_id, notice_no, source_url, "
        "started_at, finished_at, status, total_attachments, available_count, "
        "reasons, manifest_changed, manifest_change, pre_snapshot, post_snapshot, "
        "attachments) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result["verification_id"],
            result["notice_no"],
            result["source_url"],
            result["checked_at"],
            _now(),
            result["status"],
            result["total_attachments"],
            result["available_count"],
            json.dumps(result["reasons"], ensure_ascii=False),
            1 if result["manifest_changed"] else 0,
            json.dumps(_change_json(result["manifest_change"]),
                       ensure_ascii=False),
            json.dumps([e.model_dump() for e in result["pre_snapshot"]],
                       ensure_ascii=False),
            json.dumps([e.model_dump() for e in result["post_snapshot"]],
                       ensure_ascii=False),
            json.dumps([i.model_dump() for i in result["attachments"]],
                       ensure_ascii=False),
        ),
    )
    conn.commit()


def _change_json(change: ManifestChange) -> dict:
    return change.model_dump()


def list_verifications(notice_no: str | None, limit: int) -> list[dict]:
    """读取历史核验记录（可按公告编号过滤）。"""
    conn = db.connect()
    db.init_db(conn)
    if notice_no:
        rows = conn.execute(
            "SELECT * FROM verification_runs WHERE notice_no = ? "
            "ORDER BY id DESC LIMIT ?",
            (notice_no, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM verification_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()

    out: list[dict] = []
    for row in rows:
        data = dict(row)
        data["manifest_changed"] = bool(data["manifest_changed"])
        for field in ("reasons", "manifest_change", "pre_snapshot",
                      "post_snapshot", "attachments"):
            data[field] = json.loads(data[field])
        out.append(data)
    return out
