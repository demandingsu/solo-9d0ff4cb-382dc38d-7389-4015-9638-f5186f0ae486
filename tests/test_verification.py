"""附件核验 API 的端到端测试与清单对比单元测试。"""
from __future__ import annotations

import hashlib

from app.parser import AttachmentLink, parse_attachments
from app import verification


def _sync(client, base_url: str) -> None:
    c, _ = client
    resp = c.post(
        "/api/v1/sync", json={"list_url": f"{base_url}/announcements"}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "success"


def test_verify_normal_notice_all_available_with_same_name_links(
    client, site_base_url
):
    c, _ = client
    _sync(client, site_base_url)

    resp = c.post("/api/v1/announcements/A-001/attachments/verify")
    assert resp.status_code == 200
    body = resp.json()

    assert body["status"] == "all_available"
    assert body["reasons"] == []
    assert body["manifest_changed"] is False
    assert body["total_attachments"] == 4
    assert body["available_count"] == 4
    assert all(item["status"] == "ok" for item in body["attachments"])

    # 同名不同链接：两个“投标格式.docx”分别核验、链接不同
    same_name = [
        item for item in body["attachments"]
        if item["filename"] == "投标格式.docx"
    ]
    assert len(same_name) == 2
    assert len({item["url"] for item in same_name}) == 2

    # 每项都给出来源链接、声明摘要与实测摘要，且两者一致
    for item in body["attachments"]:
        assert item["url"].startswith(site_base_url + "/files/")
        assert item["declared_sha256"] == item["actual_sha256"]
        assert len(item["actual_sha256"]) == 64
        assert item["reason"] is None

    # 前后两次快照内容一致
    assert body["pre_snapshot"] == body["post_snapshot"]


def test_verify_digest_mismatch_flags_the_file(client, site_base_url):
    c, _ = client
    _sync(client, site_base_url)

    resp = c.post("/api/v1/announcements/B-001/attachments/verify")
    body = resp.json()

    # 摘要不符：整次不得报全部可用
    assert body["status"] == "needs_review"
    assert "digest_mismatch" in body["reasons"]
    assert body["available_count"] == 0
    assert body["manifest_changed"] is False

    item = body["attachments"][0]
    assert item["status"] == "digest_mismatch"
    assert item["filename"] == "招标文件正文.pdf"
    assert item["declared_sha256"] == "0" * 64
    # 实测摘要为文件字节的真实 SHA-256，且原因中点名了对应文件
    assert item["actual_sha256"] != item["declared_sha256"]
    assert item["filename"] in item["reason"]
    assert item["actual_sha256"] in item["reason"]


def test_verify_manifest_change_between_reads(
    client, site_base_url, set_manifest
):
    c, _ = client
    _sync(client, site_base_url)

    # 在核验期间切换清单：前置读取后、后置读取前生效
    from app import fetcher

    original_fetch = fetcher.fetch
    detail_url = f"{site_base_url}/notice/H-001"
    calls = {"n": 0}

    def flaky_fetch(url):
        if url == detail_url:
            calls["n"] += 1
            if calls["n"] == 2:  # 后置读取
                set_manifest(True)
        return original_fetch(url)

    fetcher.fetch = flaky_fetch
    try:
        resp = c.post("/api/v1/announcements/H-001/attachments/verify")
    finally:
        fetcher.fetch = original_fetch
        set_manifest(False)

    body = resp.json()
    assert body["status"] == "needs_review"
    assert "manifest_changed" in body["reasons"]
    assert body["manifest_changed"] is True

    added = body["manifest_change"]["added"]
    assert len(added) == 1
    assert added[0]["filename"] == "补充通知.pdf"
    assert added[0]["url"].endswith("/files/H-001/supplement.pdf")
    assert body["manifest_change"]["removed"] == []
    assert body["manifest_change"]["changed"] == []

    # 已有附件本身下载核验仍然通过，但整次结果不能报全部可用
    assert body["available_count"] == body["total_attachments"]
    assert body["total_attachments"] == 1


def test_verify_detail_page_read_failure_marks_needs_review(
    client, site_base_url, set_fail
):
    c, _ = client
    _sync(client, site_base_url)
    set_fail(True)
    try:
        resp = c.post("/api/v1/announcements/H-001/attachments/verify")
    finally:
        set_fail(False)

    body = resp.json()
    assert resp.status_code == 200
    assert body["status"] == "needs_review"
    assert "detail_fetch_failed" in body["reasons"]
    assert body["attachments"] == []
    assert body["total_attachments"] == 0


def test_verify_attachment_download_failure(client, site_base_url, monkeypatch):
    c, _ = client
    _sync(client, site_base_url)

    from app import fetcher

    def broken_download(url, max_bytes=None):
        raise fetcher.FetchError(f"下载 {url} 失败：HTTP 404")

    monkeypatch.setattr(fetcher, "fetch_bytes", broken_download)

    body = c.post(
        "/api/v1/announcements/A-001/attachments/verify"
    ).json()
    assert body["status"] == "needs_review"
    assert "download_failed" in body["reasons"]
    assert body["available_count"] == 0
    assert all(item["status"] == "download_failed"
               for item in body["attachments"])
    assert all("404" in item["reason"] for item in body["attachments"])
    assert all(item["actual_sha256"] is None for item in body["attachments"])


def test_verify_missing_declared_digest(client, site_base_url, monkeypatch):
    c, _ = client
    _sync(client, site_base_url)

    original = verification.parser.parse_attachments

    def without_digest(html, source_url):
        links = original(html, source_url)
        for link in links:
            link.declared_sha256 = None
        return links

    monkeypatch.setattr(verification.parser, "parse_attachments", without_digest)

    body = c.post(
        "/api/v1/announcements/A-001/attachments/verify"
    ).json()
    assert body["status"] == "needs_review"
    assert "missing_declared_digest" in body["reasons"]
    assert body["available_count"] == 0
    # 仍完成下载并给出实测摘要
    for item in body["attachments"]:
        assert item["status"] == "missing_declared_digest"
        assert item["declared_sha256"] is None
        assert len(item["actual_sha256"]) == 64


def test_verify_unknown_notice_404(client, site_base_url):
    c, _ = client
    _sync(client, site_base_url)
    resp = c.post("/api/v1/announcements/NOPE-999/attachments/verify")
    assert resp.status_code == 404


def test_verification_runs_history(client, site_base_url):
    c, _ = client
    _sync(client, site_base_url)

    c.post("/api/v1/announcements/A-001/attachments/verify")
    c.post("/api/v1/announcements/B-001/attachments/verify")

    runs = c.get("/api/v1/verification-runs").json()["runs"]
    assert len(runs) == 2
    assert runs[0]["notice_no"] == "B-001"  # 最新在前
    assert runs[0]["status"] == "needs_review"

    filtered = c.get(
        "/api/v1/verification-runs", params={"notice_no": "A-001"}
    ).json()["runs"]
    assert len(filtered) == 1
    assert filtered[0]["notice_no"] == "A-001"
    assert filtered[0]["status"] == "all_available"
    # 持久化的逐项结果可回放
    assert len(filtered[0]["attachments"]) == 4


def test_parse_attachments_extracts_links_names_and_digests(site_base_url):
    from app import fetcher

    html = fetcher.fetch(f"{site_base_url}/notice/A-001")
    links = parse_attachments(html, f"{site_base_url}/notice/A-001")

    assert len(links) == 4
    for link in links:
        assert link.url.startswith(site_base_url + "/files/")
        assert link.filename
        assert len(link.declared_sha256) == 64

    # 与示例站真实字节核对
    from sample_site import server as sample_server

    for link in links:
        path = link.url.replace(site_base_url, "")
        expected = hashlib.sha256(sample_server.ATTACHMENT_FILES[path]).hexdigest()
        assert link.declared_sha256 == expected


def test_diff_manifests_detects_add_remove_and_change():
    before = [
        AttachmentLink("http://x/f1", "f1.pdf", "a" * 64),
        AttachmentLink("http://x/f2", "f2.pdf", "b" * 64),
    ]
    after = [
        # 同链接摘要被篡改 -> changed
        AttachmentLink("http://x/f1", "f1.pdf", "c" * 64),
        # 新增
        AttachmentLink("http://x/f3", "f3.pdf", "d" * 64),
    ]
    change = verification.diff_manifests(before, after)

    assert [e["url"] for e in change["removed"]] == ["http://x/f2"]
    assert [e["url"] for e in change["added"]] == ["http://x/f3"]
    assert change["changed"][0]["url"] == "http://x/f1"
    assert change["changed"][0]["declared_sha256"] == "c" * 64


def test_diff_manifests_identical_snapshots():
    links = [AttachmentLink("http://x/f1", "f1.pdf", "a" * 64)]
    change = verification.diff_manifests(links, [
        AttachmentLink("http://x/f1", "f1.pdf", "a" * 64)
    ])
    assert change == {"added": [], "removed": [], "changed": []}
