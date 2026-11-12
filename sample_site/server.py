"""示例公共采购公告站（仅依赖 Python 标准库）。

用于在本地/容器内演示采集 API，覆盖以下场景：
  链 A：首次 + 更正（含截止时间继承）
  链 B：首次 + 更正 + 撤销（撤销使链失效）
  链 C：并行更正（同一公告有两个后继 -> 冲突）
  链 D：引用编号不存在（missing_reference -> 冲突 + 孤立）
  链 E：引用成环（reference_cycle -> 孤立）
  链 F：更正无引用编号（missing_reference -> 孤立）
  链 G：同一项目两个独立首次公告（保持两条独立链）
  公告 H：可通过 /fail-toggle 控制详情页返回 500，验证失败保留上次结果

附件核验场景（详情页列出附件文件名、来源链接与声明 SHA-256）：
  A-001：全部附件声明摘要正确，其中两个附件同名不同链接（分别核验）
  B-001：附件声明 SHA-256 与文件实际字节不符（digest_mismatch）
  H-001：可通过 /manifest-toggle 切换附件清单（新增附件 -> 清单变化）

控制接口：
  GET /fail-toggle?enabled=1|0       开关 H-001 详情页的 500 故障
  GET /fail-status                   查询故障开关
  GET /manifest-toggle?enabled=1|0   开关 H-001 附件清单变化
  GET /manifest-status               查询清单变化开关
"""
from __future__ import annotations

import hashlib
import os
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# 每条公告：编号、项目编号、类型、发布时间、截止时间、引用编号
NOTICES: list[dict] = [
    # 链 A：正常链，COR-A-02 不带截止时间（应继承首次的截止时间）
    {"no": "A-001", "project": "PRJ-A", "type": "首次公告",
     "publish": "2026-10-20 09:00:00", "deadline": "2026-11-20 09:30:00",
     "ref": ""},
    {"no": "A-002", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-10-25 14:00:00", "deadline": "",
     "ref": "A-001"},
    {"no": "A-003", "project": "PRJ-A", "type": "更正公告",
     "publish": "2026-11-01 10:00:00", "deadline": "2026-11-25 15:00:00",
     "ref": "A-002"},

    # 链 B：撤销使整条链失效
    {"no": "B-001", "project": "PRJ-B", "type": "首次公告",
     "publish": "2026-10-21 09:00:00", "deadline": "2026-11-21 09:30:00",
     "ref": ""},
    {"no": "B-002", "project": "PRJ-B", "type": "更正公告",
     "publish": "2026-10-26 14:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": "B-001"},
    {"no": "B-003", "project": "PRJ-B", "type": "撤销公告",
     "publish": "2026-10-28 16:00:00", "deadline": "",
     "ref": "B-002"},

    # 链 C：并行更正（C-002 与 C-003 同时引用 C-001）
    {"no": "C-001", "project": "PRJ-C", "type": "首次公告",
     "publish": "2026-10-22 09:00:00", "deadline": "2026-11-22 09:30:00",
     "ref": ""},
    {"no": "C-002", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 10:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "C-001"},
    {"no": "C-003", "project": "PRJ-C", "type": "更正公告",
     "publish": "2026-10-27 15:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "C-001"},

    # 链 D：引用了不存在的公告 X-999
    {"no": "D-001", "project": "PRJ-D", "type": "更正公告",
     "publish": "2026-10-23 09:00:00", "deadline": "2026-11-23 09:30:00",
     "ref": "X-999"},

    # 链 E：成环 E-001 -> E-002 -> E-001
    {"no": "E-001", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 09:00:00", "deadline": "2026-11-24 09:30:00",
     "ref": "E-002"},
    {"no": "E-002", "project": "PRJ-E", "type": "更正公告",
     "publish": "2026-10-24 10:00:00", "deadline": "2026-11-25 09:30:00",
     "ref": "E-001"},

    # 链 F：更正公告没有引用编号
    {"no": "F-001", "project": "PRJ-F", "type": "更正公告",
     "publish": "2026-10-25 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},

    # 链 G：同一项目两个独立首次公告
    {"no": "G-001", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-26 09:00:00", "deadline": "2026-11-26 09:30:00",
     "ref": ""},
    {"no": "G-101", "project": "PRJ-G", "type": "首次公告",
     "publish": "2026-10-27 09:00:00", "deadline": "2026-11-27 09:30:00",
     "ref": ""},
    {"no": "G-102", "project": "PRJ-G", "type": "更正公告",
     "publish": "2026-10-29 09:00:00", "deadline": "2026-11-28 09:30:00",
     "ref": "G-101"},

    # H：详情页故障开关目标（默认正常）
    {"no": "H-001", "project": "PRJ-H", "type": "首次公告",
     "publish": "2026-10-30 09:00:00", "deadline": "2026-11-30 09:30:00",
     "ref": ""},
]

NOTICE_BY_NO = {n["no"]: n for n in NOTICES}

# 运行期开关（内存态，重启容器即恢复）
FAIL_ENABLED = {"H-001": False}       # H-001 详情页 500 故障
MANIFEST_CHANGED = {"H-001": False}   # H-001 附件清单变化

# 可下载的附件字节：URL 路径 -> 文件内容
ATTACHMENT_FILES: dict[str, bytes] = {
    # A-001：正常场景（含同名不同链接的两份投标格式）
    "/files/A-001/tender.pdf": b"%PDF-1.4\nA-001 tender document body\n",
    "/files/A-001/clarification.docx": b"PK\x03\x04 A-001 clarification note\n",
    "/files/A-001/bid-form.docx": b"PK\x03\x04 A-001 bid form v1\n",
    "/files/A-001/addendum/bid-form.docx":
        b"PK\x03\x04 A-001 bid form v2 from addendum\n",
    # B-001：声明摘要与实际字节不符
    "/files/B-001/tender.pdf": b"%PDF-1.4\nB-001 tender document body\n",
    # H-001：清单切换场景
    "/files/H-001/tender.pdf": b"%PDF-1.4\nH-001 tender document body\n",
    "/files/H-001/supplement.pdf":
        b"%PDF-1.4\nH-001 supplement published after toggle\n",
}

# 每个公告详情页默认列出的附件：文件名 + 文件路径
# bad_digest=True 时页面故意给出错误的声明 SHA-256
DEFAULT_ATTACHMENTS: dict[str, list[dict]] = {
    "A-001": [
        {"filename": "招标文件正文.pdf", "path": "/files/A-001/tender.pdf"},
        {"filename": "澄清说明.docx", "path": "/files/A-001/clarification.docx"},
        {"filename": "投标格式.docx", "path": "/files/A-001/bid-form.docx"},
        # 与上一条同名、不同链接，核验时须分别处理
        {"filename": "投标格式.docx",
         "path": "/files/A-001/addendum/bid-form.docx"},
    ],
    "B-001": [
        {"filename": "招标文件正文.pdf",
         "path": "/files/B-001/tender.pdf", "bad_digest": True},
    ],
    "H-001": [
        {"filename": "招标文件正文.pdf", "path": "/files/H-001/tender.pdf"},
    ],
}

# H-001 清单切换后新增的附件
H001_MANIFEST_EXTRA = [
    {"filename": "补充通知.pdf", "path": "/files/H-001/supplement.pdf"},
]

WRONG_DIGEST = "0" * 64


def get_attachments(no: str) -> list[dict]:
    """返回某公告当前应展示的附件清单（受运行期开关影响）。"""
    entries = [dict(item) for item in DEFAULT_ATTACHMENTS.get(no, [])]
    if no == "H-001" and MANIFEST_CHANGED["H-001"]:
        entries.extend(dict(item) for item in H001_MANIFEST_EXTRA)
    return entries


def declared_digest(entry: dict) -> str:
    """页面上声明的 SHA-256：错误场景返回固定错误值，否则按字节实算。"""
    if entry.get("bad_digest"):
        return WRONG_DIGEST
    return hashlib.sha256(ATTACHMENT_FILES[entry["path"]]).hexdigest()


PAGE_CSS = """
body { font-family: sans-serif; margin: 2em; color: #222; }
h1 { font-size: 1.4em; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #999; padding: 6px 10px; text-align: left; }
th { background: #f0f3f7; }
a { color: #1a56c4; }
.meta { color: #666; margin-bottom: 1em; }
.sha256 { font-family: monospace; font-size: 0.85em; word-break: break-all; }
"""


def render_list() -> str:
    rows = "\n".join(
        '<tr><td><a href="/notice/{no}">{no}</a></td>'
        '<td>{project}</td><td>{type}</td><td>{publish}</td></tr>'.format(
            no=escape(n["no"]), project=escape(n["project"]),
            type=escape(n["type"]), publish=escape(n["publish"]),
        )
        for n in NOTICES
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公共采购公告列表</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公共采购公告列表（示例站）</h1>
<p class="meta">共 {len(NOTICES)} 条公告 · 仅用于演示采集 API</p>
<table class="announcement-list">
<thead><tr><th>公告编号</th><th>项目编号</th><th>类型</th><th>发布时间</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body></html>"""


def _row(label: str, value: str) -> str:
    return (
        f"<tr><th>{escape(label)}</th>"
        f"<td>{escape(value) if value else '—'}</td></tr>"
    )


def render_attachments(no: str) -> str:
    entries = get_attachments(no)
    if not entries:
        return (
            '<h2>招标文件附件</h2>'
            '<table class="attachment-table"><tbody></tbody></table>'
        )
    rows = []
    for entry in entries:
        digest = declared_digest(entry)
        rows.append(
            '<tr><td><a class="attachment-link" href="{href}" '
            'data-sha256="{digest}">{name}</a></td>'
            '<td class="sha256">{digest}</td></tr>'.format(
                href=escape(entry["path"]), digest=escape(digest),
                name=escape(entry["filename"]),
            )
        )
    return """<h2>招标文件附件</h2>
<table class="attachment-table">
<thead><tr><th>文件名</th><th>SHA-256</th></tr></thead>
<tbody>{rows}</tbody>
</table>""".format(rows="".join(rows))


def render_detail(no: str) -> str:
    n = NOTICE_BY_NO[no]
    rows = "".join([
        _row("公告编号", n["no"]),
        _row("项目编号", n["project"]),
        _row("公告类型", n["type"]),
        _row("发布时间", n["publish"]),
        _row("投标截止时间", n["deadline"]),
        _row("引用编号", n["ref"]),
    ])
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>公告 {escape(n['no'])}</title><style>{PAGE_CSS}</style></head>
<body>
<h1>公告详情 {escape(n['no'])}</h1>
<p><a href="/announcements">← 返回列表</a></p>
<table class="detail-table"><tbody>{rows}</tbody></table>
{render_attachments(no)}
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes | str, status: int = 200,
              content_type: str = "text/html; charset=utf-8") -> None:
        payload = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path in ("/", "/announcements"):
            self._send(render_list())
            return

        if path == "/fail-toggle":
            enabled = (query.get("enabled", ["0"])[0] in {"1", "true", "yes"})
            FAIL_ENABLED["H-001"] = enabled
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/fail-status":
            enabled = FAIL_ENABLED["H-001"]
            self._send(
                f'{{"H-001_fail_enabled": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/manifest-toggle":
            enabled = (query.get("enabled", ["0"])[0] in {"1", "true", "yes"})
            MANIFEST_CHANGED["H-001"] = enabled
            self._send(
                f'{{"H-001_manifest_changed": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path == "/manifest-status":
            enabled = MANIFEST_CHANGED["H-001"]
            self._send(
                f'{{"H-001_manifest_changed": {str(enabled).lower()}}}',
                content_type="application/json; charset=utf-8",
            )
            return

        if path.startswith("/files/"):
            content = ATTACHMENT_FILES.get(parsed.path)
            if content is None:
                self._send("<h1>404 附件不存在</h1>", status=404)
                return
            self._send(content, content_type="application/octet-stream")
            return

        if path.startswith("/notice/"):
            no = path.rsplit("/", 1)[-1]
            notice = NOTICE_BY_NO.get(no)
            if notice is None:
                self._send("<h1>404 公告不存在</h1>", status=404)
                return
            if FAIL_ENABLED.get(no):
                self._send("<h1>500 服务器内部错误（模拟故障）</h1>", status=500)
                return
            self._send(render_detail(no))
            return

        self._send("<h1>404</h1>", status=404)

    def log_message(self, fmt: str, *args) -> None:  # 精简日志
        print(f"[sample-site] {self.address_string()} {fmt % args}")


def main() -> None:
    port = int(os.getenv("SAMPLE_PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"[sample-site] 示例公告站监听 0.0.0.0:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
