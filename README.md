# 招投标公告采集 API

面向招投标专员的公共采购公告采集服务：同步指定公告列表页与详情页，抽取
**公告编号、项目编号、公告类型、发布时间、投标截止时间、引用编号**，沿引用链
归并首次公告、更正公告与撤销公告，自动识别缺失引用、引用成环、并行更正等冲突，
并输出每条链的状态、有效截止时间与来源链接。

技术栈：FastAPI + BeautifulSoup4 + SQLite，Docker Compose 同时启动采集 API
与一个内置的示例公告站。

## 目录结构

```
.
├── app/                    # 采集 API
│   ├── config.py           # 环境变量配置
│   ├── db.py               # SQLite 连接与表结构
│   ├── fetcher.py          # HTTP 抓取（超时 + 有限重试）
│   ├── parser.py           # 列表页/详情页解析（BeautifulSoup）
│   ├── chains.py           # 引用链归并、冲突识别、有效截止时间计算
│   ├── service.py          # 同步编排与持久化（含失败保留上次结果）
│   ├── verification.py     # 附件核验：前后双读详情页、下载并比对 SHA-256
│   ├── models.py           # 请求/响应模型
│   └── main.py             # FastAPI 路由
├── sample_site/            # 示例公告站（仅 Python 标准库）
│   ├── server.py
│   └── Dockerfile
├── tests/                  # pytest 测试（30 个用例）
├── Dockerfile              # API 镜像
├── docker-compose.yml      # api + sample-site 两个服务
└── requirements.txt
```

## 快速开始（Docker Compose）

```bash
docker compose up --build
```

- 采集 API：http://localhost:8000 （交互式文档 /docs）
- 示例公告站：http://localhost:8080/announcements

触发一次同步：

```bash
curl -X POST http://localhost:8000/api/v1/sync \
  -H 'Content-Type: application/json' \
  -d '{"list_url": "http://sample-site:8080/announcements"}'
```

查询引用链与孤立异常公告：

```bash
curl http://localhost:8000/api/v1/chains
```

对已同步公告核验招标文件附件（以下分别演示正常、摘要不符、清单变化场景）：

```bash
# A-001：附件全部可下载且摘要一致（含同名不同链接的两份“投标格式.docx”）
curl -X POST http://localhost:8000/api/v1/announcements/A-001/attachments/verify

# B-001：页面声明的 SHA-256 与文件实际字节不符 -> needs_review，并点名对应文件
curl -X POST http://localhost:8000/api/v1/announcements/B-001/attachments/verify

# 先开启 H-001 的清单变化开关，再核验 -> 前后两次读取发现新增附件 -> needs_review
curl "http://localhost:8080/manifest-toggle?enabled=1"
curl -X POST http://localhost:8000/api/v1/announcements/H-001/attachments/verify
curl "http://localhost:8080/manifest-toggle?enabled=0"

# 历史核验记录（可按公告编号过滤）
curl "http://localhost:8000/api/v1/verification-runs?notice_no=A-001"
```

SQLite 数据文件存放在 Docker 卷 `api-data`（容器内 `/data/procurement.db`），
容器重建后数据不丢失。

## 本地开发（无 Docker 时）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 终端 1：示例公告站
SAMPLE_PORT=8080 python -I sample_site/server.py

# 终端 2：API
DB_PATH=./data/procurement.db uvicorn app.main:app --reload --port 8000

# 测试
pytest -q
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查 |
| POST | `/api/v1/sync` | 同步指定列表页及其全部详情页，请求体 `{"list_url": "..."}` |
| GET | `/api/v1/chains` | 返回全部引用链 `chains` 与孤立异常公告 `orphans` |
| GET | `/api/v1/announcements/{notice_no}` | 查询单条公告（含抓取错误、链状态） |
| POST | `/api/v1/announcements/{notice_no}/attachments/verify` | 核验该公告的招标文件附件 |
| GET | `/api/v1/verification-runs` | 附件核验执行记录（可按 `notice_no` 过滤） |
| GET | `/api/v1/sync-runs?limit=20` | 同步执行记录 |

`/api/v1/chains` 响应示例（节选）：

```json
{
  "chains": [
    {
      "chain_id": "A-001",
      "project_no": "PRJ-A",
      "status": "ok",
      "effective_deadline": "2026-11-25 15:00:00",
      "announcements": [
        {
          "notice_no": "A-001",
          "notice_type": "first",
          "deadline": "2026-11-20 09:30:00",
          "ref_notice_no": null,
          "source_url": "http://sample-site:8080/notice/A-001",
          "conflict_reasons": [],
          "effective_deadline": "2026-11-25 15:00:00"
        }
      ]
    }
  ],
  "orphans": [
    {
      "notice_no": "D-001",
      "status": "orphan",
      "conflict_reasons": ["missing_reference"],
      "ref_notice_no": "X-999",
      "effective_deadline": null,
      "source_url": "http://sample-site:8080/notice/D-001"
    }
  ]
}
```

## 业务规则

### 链归并

- 公告类型映射：含「首次」→ `first`，含「更正」→ `correction`，
  含「撤销」→ `revocation`。
- 更正/撤销通过「引用编号」指向上一条公告；沿引用反向追溯，链标识取链上
  首次公告的公告编号。
- **同一项目编号下的不同首次公告保持独立**，不做合并（示例中 PRJ-G 有两条链）。
- 链状态：
  - `ok`：结构正常，返回有效截止时间；
  - `cancelled`：链上存在撤销公告，整条链失效，有效截止时间为 `null`；
  - `conflict`：链上存在冲突，**不推测截止时间**，有效截止时间为 `null`。
- 有效截止时间：取链上明确给出的最新截止时间；更正未给出截止时间时沿用链上
  此前的截止时间（继承已有数据，而非推测）。
- 撤销公告之后又挂接的公告不再属于该链，作为孤立异常公告处理
  （原因 `entry_after_revocation`）。

### 冲突识别（冲突即不推测截止时间）

| 冲突原因 | 触发条件 |
| --- | --- |
| `missing_reference` | 引用编号在库中不存在；或更正/撤销公告未填引用编号 |
| `reference_cycle` | 引用关系成环（含自环） |
| `parallel_correction` | 同一公告被两个及以上公告引用（并行更正） |
| `first_notice_with_reference` | 首次公告却携带引用编号（结构异常） |

无法归入任何链的公告进入 `orphans`，每条都带状态、冲突原因、来源链接，
且有效截止时间为 `null`。

### 详情抓取失败的容错

- 列表页抓取失败：本次同步标记 `failed`，不动库内现有数据。
- 单条详情页抓取/解析失败：
  - 此前成功过 → **保留上次完整结果**，仅记录 `last_fetch_error` 与同步时间，
    同步状态为 `partial`，响应中 `kept_last` 计数；
  - 从未成功 → 写入占位记录并归入孤立异常，避免静默丢失。
- 每次同步后基于库内现有数据全量重算链状态。

### 附件核验（投标标书准备前的可下载性与摘要核对）

`POST /api/v1/announcements/{notice_no}/attachments/verify` 输入**已同步**的
公告编号，读取其来源详情页上的附件链接、文件名与站点声明的 SHA-256，逐项
下载文件并**按字节**计算实测摘要。流程：

1. **核验前**读取一次详情页，解析附件清单（链接、文件名、声明摘要）；
2. 逐项下载附件，流式计算 SHA-256（受 `ATTACHMENT_MAX_BYTES` 体积上限保护）；
3. **核验后**再次读取同一详情页，对比前后两份清单；
4. 逐项返回来源链接、声明摘要、实测摘要与异常原因，结果落库可回放。

整次结果只有两种状态：

- `all_available`：清单无变化，且每个附件都下载成功、声明摘要存在且与实测
  摘要完全一致；
- `needs_review`：出现下列任一情况即为此状态，**绝不会报全部可用**。

| 异常原因代码 | 触发条件 |
| --- | --- |
| `detail_fetch_failed` | 核验前或核验后详情页读取/解析失败 |
| `manifest_changed` | 前后两次读取的附件链接、文件名或声明摘要发生变化 |
| `digest_mismatch` | 某附件声明 SHA-256 与下载字节的实测摘要不符（原因中点名对应文件） |
| `missing_declared_digest` | 详情页未给出该附件的声明 SHA-256 |
| `download_failed` | 附件下载失败（含 4xx/5xx、网络错误、超过体积上限） |
| `no_attachments_listed` | 详情页上没有列出任何附件 |

其他规则：

- **同名不同链接分别核验**：清单按附件链接去重，相同文件名的不同链接是
  独立条目，各自给出声明/实测摘要与状态。
- 清单差异以链接为键输出 `added` / `removed` / `changed`（同链接的文件名或
  声明摘要变化归入 `changed`），并保留核验前后两份快照。
- 附件下载对网络错误与 5xx 有限重试，4xx 立即失败。
- 未同步过的公告编号返回 `404`；此前详情从未抓取成功、缺少来源链接的公告
  直接标记 `needs_review`（`detail_fetch_failed`）。

## 示例公告站内置场景

| 编号 | 场景 | 预期结果 |
| --- | --- | --- |
| A-001 ~ A-003 | 首次 + 两条更正（中间一条无截止时间） | `ok`，有效截止取最新，中间项继承 |
| B-001 ~ B-003 | 首次 + 更正 + 撤销 | `cancelled`，有效截止为 null |
| C-001 ~ C-003 | 两条更正并行引用同一首次公告 | `conflict`（parallel_correction） |
| D-001 | 引用不存在的 X-999 | 孤立，missing_reference |
| E-001 / E-002 | 互相引用成环 | 孤立，reference_cycle |
| F-001 | 更正公告无引用编号 | 孤立，missing_reference |
| G-001 / G-101（+G-102） | 同项目两个独立首次公告 | 两条独立链 |
| H-001 | 可切换 500 故障 | 验证失败保留上次结果 |
| A-001 附件 | 4 个附件声明摘要全部正确，含两个同名不同链接的「投标格式.docx」 | `all_available`，同名条目分别核验 |
| B-001 附件 | 页面声明的 SHA-256 与文件实际字节不符 | `needs_review`（digest_mismatch，点名对应文件） |
| H-001 附件 | 可切换附件清单（新增「补充通知.pdf」） | 核验期间切换 -> `needs_review`（manifest_changed） |

故障开关（仅示例站）：

```bash
curl "http://localhost:8080/fail-toggle?enabled=1"   # 开启 H-001 详情 500
curl "http://localhost:8080/fail-toggle?enabled=0"   # 恢复
curl "http://localhost:8080/fail-status"
curl "http://localhost:8080/manifest-toggle?enabled=1"  # H-001 附件清单新增一项
curl "http://localhost:8080/manifest-toggle?enabled=0"  # 恢复原清单
curl "http://localhost:8080/manifest-status"
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DB_PATH` | `/data/procurement.db` | SQLite 文件路径 |
| `FETCH_TIMEOUT` | `10` | 单次 HTTP 超时秒数 |
| `FETCH_RETRIES` | `2` | 网络错误/5xx 的重试次数（4xx 不重试） |
| `RETRY_BACKOFF` | `0.5` | 重试退避基数秒 |
| `KEEP_LAST_ON_DETAIL_FAILURE` | `true` | 详情失败时保留上次完整结果 |
| `ATTACHMENT_MAX_BYTES` | `52428800`（50MB） | 单个附件下载的体积上限，超过按下载失败处理 |
| `SAMPLE_PORT`（仅示例站） | `8080` | 示例站监听端口 |
