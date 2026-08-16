# 知识星球爬虫 & 研报数据链路使用文档

> 对应 PRD: F4 研报一致性校验 (M4) 的数据采集端.
> 数据流: 知识星球 (话题/附件) → SQLite (本地) → PostgreSQL `fin.report_meta` (研报仓库)

---

## 1. 整体架构

```
┌────────────────────────────────────────────────────────────────┐
│ 知识星球 (api.zsxq.com)                                         │
│   /v2/groups/{gid}/topics      话题列表                         │
│   /v2/topics/{tid}/comments    评论                             │
│   /v2/groups/{gid}/files      群文件                            │
└───────────────┬────────────────────────────────────────────────┘
                │ (crawler + anti_detect: 随机UA/延迟/长休眠)
┌───────────────▼────────────────────────────────────────────────┐
│ SQLite (本地, output/{group_id}/)                               │
│   topics_{gid}.db   → topics / comments / users / topic_files  │
│   files_{gid}.db    → files (群文件)                            │
└───────────────┬────────────────────────────────────────────────┘
                │ (sync_zxsq_to_pg.py: 断点续传 + 幂等)
┌───────────────▼────────────────────────────────────────────────┐
│ PostgreSQL: fin schema                                          │
│   fin.report_meta      研报元数据 + 提取文本                     │
│   fin.report_forecast  研报预测抽取 (F4 校验用, 后续填充)         │
│   fin.report_sync_meta 同步水位线 (last_topic_id / last_file_id)│
│   fin.v_report_ready  已提取且有标的股票的研报视图               │
└────────────────────────────────────────────────────────────────┘
```

## 2. 配置

`.env` (`config.yaml` 不存密钥):

```env
# 知识星球 (可选, 也可用 accounts.db 管理多个账号)
ZSXQ_COOKIE=你的cookie
ZSXQ_GROUP_ID=群组ID
```

Cookie 获取: 登录 wx.zsxq.com, DevTools → Network → 任意请求 → Cookie。

多个账号: 用 `POST /accounts` 或 CLI 菜单 8 添加, 群组可绑定账号。

## 3. 运行爬虫

### 方式一: CLI (交互)

```bash
.venv/Scripts/python.exe -m cli.interactive
# 菜单: 1最新 2历史 3增量 4智能更新 5收集文件列表 6下载文件 7统计 8账号
```

### 方式二: API (后台任务)

```bash
uvicorn app:app --port 8208
```

| 端点 | 说明 |
|------|------|
| `POST /crawl/latest` | 爬最新话题 `{"group_id","per_page"}` |
| `POST /crawl/historical` | 爬历史 `{"group_id","pages","per_page"}` |
| `POST /crawl/incremental` | 增量 |
| `POST /files/collect` | 收集群文件列表 |
| `POST /files/download` | 下载文件 (每次1个, 强制间隔) |
| `GET /crawl/status` | 后台任务状态 |
| `GET /stats/{group_id}` | 本地话题库统计 |

> 反检测: 随机 User-Agent / 2-7s 间隔 / 每 15 页长休眠 3-5 分钟;
> 文件下载每次至少间隔 1-3 分钟。**勿擅自调小, 避免封号。**

## 4. 同步到 PG 研报库

```bash
# 指定群组
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg --group 123456

# 或使用 .env 的 ZSXQ_GROUP_ID
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg

# 只处理新增 N 条 (调试)
.venv/Scripts/python.exe -m scripts.sync_zxsq_to_pg --group 123456 --limit 50
```

同步内容:

1. **话题文本** → `source='zsxq_topic'` (正文即研报内容的场景)
2. **话题附件** (topic_files) → `source='zsxq'`, 本地已下载则提取 PDF/DOCX/TXT 文本 (`extraction_status='extracted'`); 未下载但有长话题文本则 `'pending'` 待补

断点续传: `fin.report_sync_meta` 记录水位, 重复运行幂等, 不会重复插入。

## 5. 研报 API

| 端点 | 说明 |
|------|------|
| `GET /reports` | 研报列表 `?source=&status=&limit=` |
| `GET /reports/{id}` | 详情 `?include_text=true` 带全文 |
| `POST /reports/upload` | 手动上传研报文本 `{"ts_code","title","content_text"}` |
| `POST /crawl/sync-reports` | 后台执行同步脚本 `{"group_id"}` |

## 6. 文件格式支持

| 格式 | 提取方式 | 依赖 |
|------|---------|------|
| PDF | PyMuPDF (fitz) → pdfplumber → pypdf 降级链 | fitz 已装 |
| DOCX | python-docx → zipfile 解析 document.xml 降级 | 可选 |
| TXT/MD | utf-8 / gbk 自动探测 | 内置 |

```python
from tools.finance.report_extractor import get_extractor
text = get_extractor().extract("path/to/report.pdf")
```

## 7. 常见问题

- **同步报"话题库不存在"**: 先运行爬虫 (CLI/API) 产生 `output/{group_id}/topics_{gid}.db`
- **附件提取失败**: 文件未下载到 `output/{gid}/downloads/`, 先跑 `/files/download` 或 CLI 菜单 6
- **Stone 星球附件无 download_url**: 附件 JSON 只有 `file` 字段时, import_topic_files 会抓 `file.download_url`/`file.url`; 实在没有则需从 `get_download_url()` API 补
- **封号风险**: 保持默认间隔, 单次历史爬取建议 ≤ 100 页

## 8. 与 F4 研报校验的衔接

`fin.v_report_ready` 输出可直接喂给研报校验链路:

```sql
SELECT * FROM fin.v_report_ready WHERE ts_code = '600519.SH' ORDER BY publish_date DESC;
```

后续 M4: `ReportVerifySkill` 读取 `report_forecast` 预测项 (营收/净利/目标价), 与 `fin.income` / `fin.fina_indicator` 实际值比对计算偏差。

---

## 9. 实测接入记录 (2026-08-16)

**已配置 Cookie** 的群组:

### 群组 A: `88882452212242` (长期主义投资认知学习社区) — 长文话题
| 指标 | 数值 |
|------|------|
| 爬取话题总数 | 160 (最新20 + 历史140) |
| 评论 | 3,354 |
| 同步入库研报 | 88 篇 |
| 其中长文 (≥200字) | 61 篇 |
| 平均字数 / 最长 | 1,412 / 9,008 字 |

**群组内容形态**: 以**长文话题**为主 (市场早报/周报、策略分析、直播复盘), 无直接 PDF 附件分享。研报文本全部通过 `zsxq_topic` 通道入库。

### 群组 B: `51288148188224` (猫哥的研报圈) — 外资研报文件为主 (每日更新)
| 指标 | 数值 |
|------|------|
| 文件列表 | 100 (今天新增 94) |
| 已下载 (验证) | 2 个 PDF (7.8MB / 10.9MB) |
| 话题文本 | 20 条 (每条 300-1500 字核心观点) |
| 研报来源 | 美银/高盛/摩根大通/野村/巴克莱/伯恩斯坦 等 |

**群组内容形态**: 每天批量推送**外资券商研报** (英文原版 PDF + 中文译文 PDF + MP3 音频, 三件套同 topic)。

**关键发现**:
- 文件 JSON 字段为 `file_id` 而非 `id` (已修复下载器)
- 研报 PDF 为**扫描版无文本层**, 纯文本提取为空 — 因此研报正文以**关联话题文本** (核心观点全文) 为内容, 附件作为原始文件保存
- 文件通道: `_sync_files` (topic_files 表) 和 `_sync_group_files` (files 库) 两个通道都接入, 幂等去重

**已启用准入过滤**: 正文 < 30 字的短内容 (追问/闲聊) 自动跳过; 富文本标签 `<e type="web" href="...">` 已在同步时清洗。

### 每日四时段自动抓取 (6/12/18/24)

核心脚本 `scripts/daily_fetch_zxsq.py` 一次运行完成:
1. 爬最新话题 (研报核心观点文本)
2. 收集文件列表 (增量)
3. 下载待下载文件 (反检测长休眠, 每次限 N 个)
4. 同步 PG 研报库

```bash
# 手动运行一次 (下载10个文件)
.venv/Scripts/python.exe -m scripts.daily_fetch_zxsq --group 51288148188224 --max-fetch 10

# 仅检查今天新增
.venv/Scripts/python.exe -m scripts.daily_fetch_zxsq --group 51288148188224 --check
```

**Windows 定时任务** (4 个任务, 每天 6/12/18/24 点):
```bat
schtasks /Create /TN "ZSXQ_0600" /SC DAILY /ST 06:00 /TR "C:\path\.venv\Scripts\python.exe C:\path\scripts\daily_fetch_zxsq.py --group 51288148188224 --max-fetch 10"
schtasks /Create /TN "ZSXQ_1200" /SC DAILY /ST 12:00 /TR "... 同上 ..."
schtasks /Create /TN "ZSXQ_1800" /SC DAILY /ST 18:00 /TR "... 同上 ..."
schtasks /Create /TN "ZSXQ_2400" /SC DAILY /ST 00:00 /TR "... 同上 ..."
```

> 注意: Cookie 有时效, 失效后需重新获取并更新 `.env` (可用 `GET /crawl/status` 或直接请求群组接口验证)。