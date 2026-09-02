# ai-assistant 知识星球爬虫技术文档 (Spider)

> 爬虫层完整技术文档: 架构 / 配置 / 反检测 / 数据仓库对接 / 每日定时抓取 / 实测接入
> 对应 PRD: F4 研报一致性校验 (M4) 的数据采集端
> 更新时间: 2026-08-16
> 关联文档: `PRD.md`, `data_process.md` (数据层)

---

## 目录

- [1. 整体架构](#1-整体架构)
- [2. 核心模块](#2-核心模块)
- [3. 配置](#3-配置)
- [4. 反检测机制](#4-反检测机制)
- [5. 数据仓库对接 (SQLite → PG)](#5-数据仓库对接-sqlite--pg)
- [6. 研报文本提取](#6-研报文本提取)
- [7. 每日定时抓取](#7-每日定时抓取)
- [8. 运行爬虫 (CLI / API)](#8-运行爬虫-cli--api)
- [9. API 接口](#9-api-接口)
- [10. 实测接入情况](#10-实测接入情况)
- [11. 已修复的 Bug 清单](#11-已修复的-bug-清单)
- [12. 运维与常见问题](#12-运维与常见问题)
- [13. 与 F4 研报校验的衔接](#13-与-f4-研报校验的衔接)
- [14. 下一步计划](#14-下一步计划)
- [附录: 关键文件速查](#附录-关键文件速查)

---

## 1. 整体架构

```
┌──────────────────────────────────────────────────────────────────┐
│ 知识星球 (api.zsxq.com)                                           │
│   /v2/groups/{gid}/topics     话题列表 (正文/附件/评论)            │
│   /v2/topics/{tid}/comments   评论                                 │
│   /v2/groups/{gid}/files      群文件列表 (研报 PDF/MP3)            │
│   /v2/files/{fid}/download_url  文件下载直链                       │
└──────────────┬───────────────────────────────────────────────────┘
               │ tools/zsxq (反检测: 随机 UA / 延迟 / 长休眠)
┌──────────────▼───────────────────────────────────────────────────┐
│ 本地 SQLite (output/{group_id}/)                                  │
│   topics_{gid}.db    topics / comments / users / topic_files      │
│   files_{gid}.db     files (群文件索引) + collection_log           │
│   downloads/         已下载的原始文件 (PDF/MP3)                    │
└──────────────┬───────────────────────────────────────────────────┘
               │ scripts/sync_zxsq_to_pg.py (断点续传 + 幂等)
┌──────────────▼───────────────────────────────────────────────────┐
│ PostgreSQL: fin schema                                            │
│   fin.report_meta      研报元数据 + 提取文本 (F4 校验数据源)        │
│   fin.report_forecast  研报预测抽取 (M4 待做)                      │
│   fin.report_sync_meta 同步水位线 (last_topic_id / last_file_id)   │
│   fin.v_report_ready  已提取且有标的股票的研报视图                  │
└───────────────────────────────────────────────────────────────────┘
```

数据流约定:
- **爬取层** 只写入本地 SQLite（原始、可重放），**不直接写 PG**
- **同步层** 负责 SQLite → PG 的增量迁移（幂等、断点续传）
- **PG 研报库** 是 F4 研报校验 / F5 报告生成的数据源

## 2. 核心模块

| 模块 | 路径 | 职责 |
|------|------|------|
| 话题爬虫 | `tools/zsxq/crawler.py` | 话题/评论爬取 (latest/historical/incremental) |
| 文件下载器 | `tools/zsxq/downloader.py` | 群文件列表收集 + 文件下载 (含强制间隔) |
| 反检测 | `tools/zsxq/anti_detect.py` | 随机 UA / 请求头 / 延迟 / 长休眠 |
| 话题库 | `storage/sqlite/topics.py` | topics/comments/users/topic_files 表 |
| 文件库 | `storage/sqlite/files.py` | files 表 + collection_log + 状态流转 |
| 文本清洗 | `utils/helpers.py::strip_zsxq_tags` | `<e type="web" href="...">` 富文本清理 |
| 文本提取 | `tools/finance/report_extractor.py` | PDF/DOCX/TXT/MD → 纯文本 |
| 同步脚本 | `scripts/sync_zxsq_to_pg.py` | SQLite → PG 研报库迁移 |
| 每日抓取 | `scripts/daily_fetch_zxsq.py` | 定时任务入口 (爬话题 + 收集 + 下载 + 同步) |
| 账号管理 | `storage/sqlite/account.py` | Cookie 账号 (accounts.db, 支持多账号/群组绑定) |

## 3. 配置

`.env` (`config.yaml` 不存密钥):

```env
# 知识星球 (可选, 也可用 accounts.db 管理多个账号)
ZSXQ_COOKIE=你的cookie
ZSXQ_GROUP_ID=51288148188224
```

**Cookie 获取**: 登录 wx.zsxq.com → DevTools → Network → 任意请求 → Cookie。

**多账号**: 用 `POST /accounts` 或 CLI 菜单 8 添加，群组可绑定账号（`group_account_map` 表）。

**当前默认群组**: `51288148188224` (猫哥的研报圈，每日四时段抓取主目标)。

## 4. 反检测机制

**设计目标**: 模拟正常用户操作，避免触发知识星球风控（封号风险极高）。

| 机制 | 参数 | 说明 |
|------|------|------|
| User-Agent 轮换 | 5 个浏览器 UA | 随机选择 (Chrome/Firefox/Edge) |
| 请求间隔 | 普通 2-5s, 历史 3-7s | `random.uniform` 抖动 |
| 长休眠 | 每 15 页 60-120 秒 | 模拟人类暂停 |
| 文件下载间隔 | 60-120 秒/个 | 强制（防高频下载） |
| 长休眠 (下载后) | 60-120 秒 | 大文件下载后暂停 |
| 请求头完整化 | Cookie + X-Request-Id + Sec-Ch-Ua 等 | 模拟真实浏览器 |
| 可中断 | 支持停止信号 | 长休眠可被中断 |

**自定义间隔** (调试用，默认禁止):
```python
crawler.set_custom_intervals(crawl_interval_min=2.0, crawl_interval_max=3.0)
```

**风险红线**: 单次历史爬取建议 ≤ 100 页; 单次下载建议 ≤ 20 个文件; Cookie 失效立即停止。**勿擅自调小间隔，避免封号。**

## 5. 数据仓库对接 (SQLite → PG)

### 5.1 本地 SQLite (output/{group_id}/)

**topics_{gid}.db** (话题库):
```sql
CREATE TABLE topics        (topic_id PK, group_id, type, title, create_time,
                            digested, sticky, likes_count, comments_count,
                            reading_count, text, imported_at);
CREATE TABLE comments      (comment_id PK, topic_id, owner_user_id, parent_comment_id,
                            text, create_time, likes_count);
CREATE TABLE users         (user_id PK, name, alias, avatar_url, location, description);
CREATE TABLE topic_files   (file_id PK, topic_id, name, size, file_type,
                            download_url, create_time, download_status, local_path);
```

**files_{gid}.db** (群文件库):
```sql
CREATE TABLE files         (file_id PK, topic_id, name, size, hash,
                            download_count, create_time, download_status,
                            local_path, download_time);
CREATE TABLE collection_log(id PK, start_time, end_time, total_files, new_files, status);
```

### 5.2 PG 研报表 (fin schema)

```sql
CREATE TABLE fin.report_meta (
    report_id SERIAL PK,
    topic_id BIGINT,                    -- 知识星球话题ID
    file_id BIGINT,                     -- 知识星球文件ID
    ts_code VARCHAR(16),                -- 标的股票 (F4 抽取后填充)
    title VARCHAR(256),
    author VARCHAR(64), org_name VARCHAR(64),
    publish_date DATE,
    report_type VARCHAR(16),            -- deep/comment/weekly
    source VARCHAR(32),                 -- zsxq / zsxq_topic / zsxq_file / upload
    file_path TEXT,                     -- 本地文件路径
    file_name VARCHAR(256), file_size BIGINT,
    content_text TEXT,                  -- 提取的文本
    content_chars INTEGER,              -- 文本长度 (质量检查)
    extraction_status VARCHAR(16),      -- pending / extracted / failed
    created_at TIMESTAMP
);
CREATE TABLE fin.report_forecast (...);  -- M4 研报预测抽取 (待做)
CREATE TABLE fin.report_sync_meta (...); -- 同步水位线
```

### 5.3 同步链路 (scripts/sync_zxsq_to_pg.py)

三条数据通道（按需执行，幂等）:

| 通道 | 源 | 触发条件 | source |
|------|-----|---------|--------|
| `_sync_topics` | topics 表 | 话题正文 (长文研报) | `zsxq_topic` |
| `_sync_files` | topic_files 表 | 话题内附件 (研报 PDF) | `zsxq` |
| `_sync_group_files` | files 表 | 群文件列表 (独立研报文件) | `zsxq_file` |

**幂等保证**:
- 同步前查 `fin.report_meta` 是否已存在 (`source + topic_id` 或 `file_id`)
- `fin.report_sync_meta` 记录水位 (`last_topic_id` / `last_file_id` / `group_file_id_{gid}`)
- 水位按**实际最大主键**推进 (非累加), 防止边界死循环

**准入过滤**:
- 正文 < 30 字的短内容 (追问/闲聊) 自动跳过
- `<e type="web" href="...">` 富文本标签同步前清洗
- 话题附件未下载时降级用话题文本 (`pending` 状态)

**手动同步**:
```bash
# 指定群组
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg --group 51288148188224

# 或使用 .env 的 ZSXQ_GROUP_ID
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg

# 只处理新增 N 条 (调试)
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg --group 51288148188224 --limit 50
```

## 6. 研报文本提取

`tools/finance/report_extractor.py` 统一处理各类研报文件:

| 格式 | 提取方式 | 依赖 |
|------|---------|------|
| PDF | PyMuPDF (fitz) → pdfplumber → pypdf 降级链 | fitz 已装 |
| DOCX | python-docx → zipfile 解析 document.xml 降级 | 可选 |
| TXT/MD | utf-8 / gbk 自动探测 | 内置 |

**使用**:
```python
from tools.finance.report_extractor import get_extractor
text = get_extractor().extract("path/to/report.pdf")
```

**扫描版 PDF 的处理**:
- 猫哥群的研报 PDF 为**扫描图片版** (16 页 fitz 提取文本为空)
- 当前策略: **用关联话题文本 (核心观点全文) 作为研报正文**, PDF 附件作为原始文件保存
- OCR 支持 (PaddleOCR) 未引入 (依赖较重, 后续按需加)

## 7. 每日定时抓取

### 7.1 后端调度器 (生产环境, 推荐)

`core/zsxq_scheduler.py` 是随 FastAPI 后端常驻的 **asyncio 定时任务**, 无需额外 cron/schtasks:

- **调度时点**: 默认每天 **07:00 与 23:00** 各跑一轮 (`config.yaml` → `zsxq_schedule.run_hours`, 可改)
- 启动补跑: 开机晚于 07:00 且当天未跑时立即补跑一轮
- 每轮流程: 爬话题 → 收集+下载文件 (限额) → 同步 PG → LLM 元数据/深度提取
- 幂等: 话题/文件按 ID 查重, PG 同步断点续传, LLM 提取按 analysis_status

调度器每轮复用 `daily_fetch_zxsq.run_once`:

```
爬最新话题 (研报核心观点) → 收集文件列表 (增量) → 下载文件 (限N个) → 同步PG研报库
```

### 7.2 手动运行

```bash
# 完整抓取 (下载10个文件)
.venv/Scripts/python.exe -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10

# 仅检查今天新增
.venv/Scripts/python.exe -m scripts.daily_fetch_zxsq --group 51288148188224 --check

# 调试: 跳过下载
.venv/Scripts/python.exe - <<'PY'
from scripts.daily_fetch_zxsq import run_once
run_once("51288148188224", max_fetch=0)
PY
```

### 7.3 外部定时 (备选: 不用后端常驻时)

**Windows schtasks** (按需设定时段, 如 6/12/18/24):

```bat
schtasks /Create /TN "ZSXQ_0600" /SC DAILY /ST 06:00 /TR "C:\path\.venv\Scripts\python.exe C:\path\scripts\daily_fetch_zxsq.py --group 51288148188224 --max-fetch 10"
schtasks /Create /TN "ZSXQ_1200" /SC DAILY /ST 12:00 /TR "... 同上 ..."
schtasks /Create /TN "ZSXQ_1800" /SC DAILY /ST 18:00 /TR "... 同上 ..."
schtasks /Create /TN "ZSXQ_2400" /SC DAILY /ST 00:00 /TR "... 同上 ..."
```

**Linux cron**:
```cron
0 6,12,18,0 * * * cd /path/ai-assistant && .venv/bin/python -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10 >> logs/daily_fetch.log 2>&1
```

### 单次运行容量估算

| 操作 | 耗时 | 限制 |
|------|------|------|
| 爬 20 条话题 | ~1-2 分钟 | 反检测间隔 |
| 收集 10 页文件列表 | ~1-3 分钟 | 反检测间隔 |
| 下载 10 个文件 | ~10-25 分钟 | 每文件 60-120 秒间隔 |
| 同步 PG | <1 秒 | 幂等 |

**单次运行总耗时约 15-30 分钟**（具体取决于下载文件数），调度器默认每天 07:00 / 23:00 各跑一轮（可经 config.yaml `zsxq_schedule.run_hours` 覆盖）。

## 8. 运行爬虫 (CLI / API)

### 方式一: CLI (交互)

```bash
.venv/Scripts/python.exe -m cli.interactive
# 菜单: 1最新 2历史 3增量 4智能更新 5收集文件列表 6下载文件 7统计 8账号
```

### 方式二: API (后台任务)

```bash
uvicorn app:app --port 8208
```

## 9. API 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/crawl/latest` | 爬最新话题 `{"group_id","per_page"}` |
| POST | `/crawl/historical` | 爬历史 `{"group_id","pages","per_page"}` |
| POST | `/crawl/incremental` | 增量爬取 |
| GET | `/crawl/status` | 后台任务状态 |
| POST | `/files/collect` | 收集文件列表 |
| POST | `/files/download` | 下载文件 (限1个, 强制间隔) |
| POST | `/files/download-topic-files` | 下载话题附件 |
| POST | `/crawl/sync-reports` | 后台同步研报库 |
| GET | `/stats/{group_id}` | 本地话题库统计 |
| GET | `/reports` | 研报列表 `?source=&status=&limit=` |
| GET | `/reports/{id}` | 研报详情 `?include_text=true` 带全文 |
| POST | `/reports/upload` | 手动上传研报 `{"ts_code","title","content_text"}` |
| GET | `/health` | 健康检查 |

**示例**:
```bash
curl "http://localhost:8208/reports?source=zsxq_file&limit=5"
curl "http://localhost:8208/reports/207?include_text=true"
curl -X POST "http://localhost:8208/crawl/latest" -H "Content-Type: application/json" -d '{"group_id":"51288148188224","per_page":20}'
```

## 10. 实测接入情况

**目标群组** (Cookie 已配置, 付费群已加入):

### 群组 A: `51288148188224` (猫哥的研报圈) — 外资研报文件为主 (每日更新)

| 指标 | 数值 |
|------|------|
| 文件列表 | 100 (今天新增 94) |
| 已下载 | 2 个 PDF (7.8MB / 10.9MB, 验证链路) |
| 话题文本 | 20 条 (每条 300-1500 字核心观点) |
| 研报机构 | 美银/高盛/摩根大通/野村/巴克莱/伯恩斯坦/德意志银行 |

**内容形态**: 每天批量推送**外资券商研报**（英文原版 PDF + 中文译文 PDF + MP3 音频，三件套同 topic）。

**关键发现**:
- 文件 JSON 字段为 `file_id` 而非 `id`（已修复下载器）
- 研报 PDF 为**扫描版无文本层**（fitz 提取为空），正文以关联话题文本为主
- 两个文件通道（topic_files / files）并存，幂等去重
- 多个文件共享同一 topic 时，该 topic 无正文（纯附件说明）

### 群组 B: `51115521812114` (纳指星球调研) — 行业研报话题型

| 指标 | 状态 |
|------|------|
| 访问 | ✅ 可访问 (已加入) |
| 内容 | 申万/TMT 行业研报、公司点评、周观点 |
| 接入 | 待抓取 (按需运行 `daily_fetch_zxsq.py --group 51115521812114`) |

**内容形态**: 行业/个股研报话题（含申万消费/TMT、良信股份等公司点评），正文在话题文本中。无独立文件列表 API 访问，走 `zsxq_topic` 通道。

> 两个群组定时任务可分别配置 (不同 group_id)。当前 `.env` 默认群组为猫哥研报圈。

## 11. 已修复的 Bug 清单

| # | Bug | 文件 | 影响 | 修复 |
|---|-----|------|------|------|
| 1 | 文件字段用 `get('id')` 而非 `get('file_id')` | `tools/zsxq/downloader.py` | 文件 ID 全为 None，无法下载 | 改兼容 `file_id` 或 `id` |
| 2 | `FilesDatabase` 缺 `commit()` 方法 | `storage/sqlite/files.py` | 收集文件列表首次崩溃 | 补 `commit()` |
| 3 | `download_pending` 硬编码 `max_files=1` | `tools/zsxq/downloader.py` | 覆盖传入参数，一天只下1个 | 解除限制（上限 20） |
| 4 | `daily_fetch_zxsq.py` 文件末 `"""` 注释含 `C:\Users` | `scripts/daily_fetch_zxsq.py` | 启动即 SyntaxError | 改 `#` 注释 |
| 5 | 下载文件名与库中路径不一致 | `tools/zsxq/downloader.py` | `local_path` 存原始名但磁盘存 sanitize 名 | 同步时按 sanitize 路径兜底 |
| 6 | 同步水位按 `total` 累加 | `scripts/sync_zxsq_to_pg.py` | 有 `continue` 跳过时会死循环 | 按实际最大主键推进 |
| 7 | 话题附件不入库 | `storage/sqlite/topics.py` | 话题 JSON 里的 `talk.files` 丢失 | 新增 `topic_files` 表 + `import_topic_files` |
| 8 | 短内容/无文本话题误入库 | `scripts/sync_zxsq_to_pg.py` | 70+ 条 `content_chars=0` 垃圾数据 | 准入过滤（正文 ≥30 字） |

## 12. 运维与常见问题

### Q: Cookie 在哪里配置？
`.env` 中 `ZSXQ_COOKIE=...` 和 `ZSXQ_GROUP_ID=...`。Cookie 从 wx.zsxq.com DevTools 获取，有效期有限，失效后需更新。

### Q: 如何验证 Cookie 有效？
```bash
curl "http://localhost:8208/reports"  # 或直接用 Python 请求群组接口
```

### Q: 同步报"话题库不存在"怎么办？
先运行爬虫（CLI/API）产生 `output/{group_id}/topics_{gid}.db`，再跑同步。

### Q: 附件提取失败怎么办？
文件未下载到 `output/{gid}/downloads/`。先跑 `/files/download` 或 CLI 菜单 6。

### Q: 话题附件无 download_url？
附件 JSON 只有 `file` 字段时，`import_topic_files` 会抓 `file.download_url`/`file.url`；实在没有则需从 `get_download_url()` API 补。

### Q: 扫描版 PDF 提取不出文本怎么办？
当前策略是**用关联话题文本**作为研报正文（核心观点全文已入库）。如需全文 OCR，可后续引入 PaddleOCR（依赖较重，需评估）。

### Q: 单次运行太慢怎么办？
- `--max-fetch 5` 减少单次下载量
- 反检测间隔是硬约束（防封号），不建议缩短
- 下载大文件（>10MB）会触发长休眠，属正常

### Q: 两个群组的数据混在一起吗？
**不混**。PG 里 `source` 和 `topic_id`/`file_id` 唯一标识来源；SQLite 按群组目录隔离 (`output/{gid}/`)。

### Q: 重复运行会重复入库吗？
**不会**。同步前查 PG 是否已存在，且 `fin.report_sync_meta` 记录水位线。多次运行幂等。

## 13. 与 F4 研报校验的衔接

`fin.v_report_ready` 输出可直接喂给研报校验链路:

```sql
SELECT * FROM fin.v_report_ready WHERE ts_code = '600519.SH' ORDER BY publish_date DESC;
```

后续 M4: `ReportVerifySkill` 读取 `report_forecast` 预测项 (营收/净利/目标价), 与 `fin.income` / `fin.fina_indicator` 实际值比对计算偏差。

## 14. 下一步计划

### 短期 (M4 前置)
1. **定时任务落地**: 在 Windows schtasks / Linux cron 配置 4 个每日任务
2. **多群组支持**: `daily_fetch_zxsq.py` 支持一次跑多个群组 (猫哥 + 纳指星球)
3. **OCR 支持**: 对扫描版 PDF 做全文提取 (PaddleOCR / RapidOCR)，提升 failed 文件可用率
4. **研报正文结构化**: 从话题文本中提取「标题/机构/日期/核心观点/盈利预测」结构化字段（供 F4 直接消费）

### 中期 (M4)
5. **研报预测抽取**: `fin.report_forecast` 填充（营收/净利/目标价预测，用 LLM + 规则）
6. **研报与财务数据校验**: `ReportVerifySkill` 实现（预测 vs 实际比对）

### 长期
7. **增量优化**: 爬取时按 `end_time` 精确过滤（当前按页数，可能多爬）
8. **质量监控**: 每日抓取成功率 / 失败原因 / 磁盘占用告警
9. **多账号轮询**: 用 accounts.db 实现多 Cookie 轮换（防单账号风控）

---

## 附录: 关键文件速查

| 要改什么 | 文件 |
|---------|------|
| 反检测间隔 | `tools/zsxq/anti_detect.py` |
| 话题爬取逻辑 | `tools/zsxq/crawler.py` |
| 文件下载逻辑 | `tools/zsxq/downloader.py` |
| 话题/附件入库 | `storage/sqlite/topics.py` |
| 群文件入库 | `storage/sqlite/files.py` |
| 富文本清洗 | `utils/helpers.py::strip_zsxq_tags` |
| 文本提取 | `tools/finance/report_extractor.py` |
| SQLite → PG 同步 | `scripts/sync_zxsq_to_pg.py` |
| 每日定时抓取 | `scripts/daily_fetch_zxsq.py` |
| API 端点 | `api/router.py` |
| PG 表结构 | `storage/pg_schema.py` (report_meta / report_forecast) |
