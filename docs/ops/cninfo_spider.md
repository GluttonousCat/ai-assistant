# 巨潮资讯爬虫（定期报告 PDF）设计文档

> Alpha Finance Radar · 数据获取子系统（tools/cninfo，自包含模块）
> 更新：2026-10-11（解耦重构 + batch 并入 downloader + parse/evidence 迁入）

## 一、系统设计

### 1.1 定位

从巨潮资讯网（cninfo，官方指定信息披露平台）拉取上市公司定期报告原文
（AR 年报 / H1 半年报 / Q1 一季报 / Q3 三季报 PDF）并解析。
**自包含模块**：与宿主项目零耦合（整目录可独立拷出，运行依赖 requests；
解析另需 pymupdf4llm/pymupdf，降级链 pdfplumber/pypdf；指数成分批量另需 tushare）。

用途：年报交叉核验（报告原文 vs Tushare 财务数据）、全文问答
（`extract_document`）、研究生产线证据源（items.json → annual_source）。

### 1.2 模块结构（2026-10-11 定型）

```
tools/cninfo/
  client.py      纯 HTTP 层: orgId 解析 / 公告检索(翻页) / 全市场清单(szse_stock.json)
                 / PDF 下载 (直连绕代理 + 随机延迟 + 指数退避 + Range 断点续传
                 + %PDF 魔数校验)
  filters.py     筛选纯函数: 类别识别 / 报告年度解析 / 干扰版排除 / 代码归一化
                 normalize_ts_code (6位代码→ts_code, 名称解析归调用方)
  store.py       文件存储: org_map.json + 单股 <ts_code>.json (公告元数据+断点 state)
  downloader.py  获取编排 + CLI 单入口: 单股 CninfoDownloader / 批量 BatchCrawler
                 (断点续传 + 限流熔断 + 时间预算; 2026-10 batch.py 并入)
  parse.py       报告解析 + CLI: PDF→MD→{items,sections} JSON (AR/H1);
                 annual_corpus 确定性语料提炼
  evidence.py    年报原子证据抽取内核: 系统提示词/小节序列化/原子清洗
                 (LLM 客户端由调用方注入; 编排与 EvidenceShard 契约在 research 侧)
  tests/         纯函数/文件存储/解析方言单测 (无网络)
```

**存储语义（2026-10-11 与 PG 解耦）**：元数据/进度/orgId 全部为文件产物
`output/cninfo/meta/`（os.replace 原子写）；下载幂等 = **文件在盘即已下载**
（PG 时代存量 PDF 自愈回填 meta）。历史 PG 表 `fin.cninfo_*` 保留数据未迁移，
DDL 已从 pg_schema 移除。年报存储双层架构（PG 真相源 + Milvus annual_chunks）
见 [docs/agents/annual_storage.md](../agents/annual_storage.md)（定稿待实现）。

```
mcp/tools/reports.py   fetch_annual_report 工具 (自己 resolve_one 名称→代码后调用)
output/cninfo/downloads/<ts_code>/<year>_<category>.pdf
output/cninfo/md/<ts_code>/<year>_<category>.md + _items.json
```

### 1.3 端点与关键参数（实测 2026-09）

| 端点 | 用途 | 要点 |
|------|------|------|
| POST `/new/information/topSearch/query` | 代码→orgId | 沪市 `gssh0+code`（可推导），深市 `9900xxxx`（必须查询） |
| POST `/new/hisAnnouncement/query` | 公告检索 | `column=szse` **沪深通用**；`category_ndbg_szsh` 等类别参数；**必须带 orgId**（实测只传 code 返回 0） |
| GET `static.cninfo.com.cn/<adjunctUrl>` | PDF 下载 | 校验 `%PDF` 魔数防错误页；支持 Range 续传（206） |
| GET `/new/data/szse_stock.json` | 全市场清单 | code/orgId/zwjc 一次拿全（批量枚举与 orgId 批量源） |

### 1.4 关键设计

- **orgId 解析链**：meta 文件映射（一生一次）→ 内存缓存 → szse_stock.json 批量源
  → topSearch 单查 → 沪市规则推导兜底（topSearch 限流最敏感，实测高频 504）
- **披露窗口跨年**：N 年度报告在 N+1 年披露，查询窗口取
  `N-01-01 ~ N+1-12-31` 宽窗，再按标题解析的报告年度精确归属
- **幂等**：元数据按 `announcement_id`（巨潮原生）为键 upsert（保留下载回填字段）；
  下载按「文件在盘」存在性跳过（非水位线——见 AGENTS.md 踩坑表）
- **全文只挑一份**：全部公告（含摘要等）落盘留痕，下载仅取筛选器选出的正式全文
  （类别优先 年报>半年报>季报，同类取最新）；更正/更新版本默认排除，元数据可追溯
- **解析方言**（parse.py，实证见 profile_extraction_handoff.md §5）：小节切分按
  编号形态判兄弟（一、=2.5 / N.M=3.0 / （一）=3.5，# 层数不可信）；节定位双方言
  （「第X节/章」锚≥4 / 主题式动态选节级）+ 混合式补齐

## 二、需求优化

### 2.1 全量批量：元数据全量 + PDF 按需

**量级实测账**（2026-09）：全市场 ~5400 只 × 5 年 × 4 类 ≈ **10 万条公告**。
PDF 全量 ≈ **300GB** 本机放不下 → 元数据全量（meta 文件）+ PDF 按需
（关注池批量 / 对话内单只拉取）。

- 查询优化：每类别宽窗一次查询（5 年 4 类 = **4 请求/股**；分号多类别参数实测不可用）
- 限流对策：股票间 2~5s 随机延迟、指数退避重试、**连续 8 只失败熔断优雅停**、
  `--max-minutes` 时间预算分夜跑
- 断点续传：单股 meta json 的 state 键（batch 维护 meta/dl 状态）；股票枚举
  天然有序（szse_stock.json 排序后 ts_code 升序）
- 预估：健康限流下 ~15-20s/股，全量元数据约 20~30 小时，建议每晚
  `--max-minutes 120` 分夜跑

### 2.2 两段式补 PDF

meta 已 done 仅缺 PDF 的股票走 **meta 直读下载**（`download_from_meta`：按
(年,类) 挑全文条目直接下载，零查询接口调用——查询接口限流敏感而静态下载不敏感）；
`--download-index` 对指数成分批量走此路。

### 2.3 解析与证据（2026-10 迁入）

- parse.py：PDF→MD（pymupdf4llm 主路，>100 字门槛；失败/过短降级文本层三库链
  fitz→pdfplumber→pypdf）→ items（第二章 kpi3y/nonrecurring 词表+相似度兜底）
  + sections（三~七节 L1 小节全量，SECTION_RULES）；AR 无文本层时 H1 顶上。
  Q1/Q3 下载与元数据已支持，解析待另立规则（短报告无三~七节结构）。
- evidence.py：四类原子（operating/segment/statement/risk）证据抽取提示词与
  清洗链；LLM 客户端调用方注入（`.invoke(messages)->str`），编排与 EvidenceShard
  契约留 research 侧（annual_source.py）。

## 三、问题（历史问题与处理记录）

| 问题 | 根因 | 处理 |
|------|------|------|
| 系统代理下请求超时 | 走了本机代理（127.0.0.1:10808），巨潮国内站直连才通 | `session.trust_env=False` 绕代理 |
| topSearch 频繁 504 | 端点限流敏感，短时多次即触发 | orgId 文件映射+内存缓存+批量源+沪市推导兜底 + 指数退避（3/12/27/30s，4 次） |
| 「半年度报告」被误分类为年报 | "半**年度报告**"含子串"年度报告" | 类别匹配顺序 季报/半年报在前、年报最后 + 单测防回归 |
| 无 orgId 查询返回 0 | hisAnnouncement 必须带 orgId | orgId 必经路径，多级策略保障 |
| 大文件 PDF 3 次重试全废 | 30MB+ 年报每连接仅得 3-5MB 即被掐断 | Range 断点续传 + 有增量即前进（连续零增量才放弃）；绝对上限 40 次/文件 |
| batch 全市场枚举依赖 stock.stock_basic | 解耦后不能查库 | 改巨潮自家 szse_stock.json（前端下拉数据源）；无退市过滤，推不出后缀的条目跳过 |
| Windows 原子写抛 FileExistsError | `Path.rename` 目标已存在时 Win 报错（POSIX 才是替换） | store 统一 `os.replace` |
| 系统 Python 跑解析 items/sections 全空 | 缺 pymupdf4llm，降级链产出无结构纯文本 | 正式解析用 `.venv/Scripts/python.exe`（已装 pymupdf4llm） |
| PG 服务端重启杀整场批量任务（历史） | 数小时批任务握单连接 | 2026-10 与 PG 解耦后此问题类别整体消失（无库可断） |

## 四、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-08 | 批量试跑 limit 5 双轮（断点续传/熔断/退避）；E2E 查询→下载（%PDF 校验）；MCP fetch_annual_report 全程 31.8s | ✅ |
| 2026-09-10 | Range 探测回 206/100B；上证50 `--index --with-pdf` 全文下载 | ✅ |
| 2026-10-11 | 解耦重构：单股查询落盘 meta、幂等跳过自愈（300308 存量 6.97MB 回填）、cninfo 23 测 + 全仓 285 测绿 | ✅ |
| 2026-10-11 | parse/evidence 迁入：venv 实跑 300308 2024 年报 items 两项全中 + sections 四节（mdna20/治理2/事项7/股东4）；annual_corpus 2734 字；248 测绿 | ✅ |
| 2026-10-11 | batch 并入 downloader：CLI 单入口冒烟（单股/批量旗标/指数模式 help 正常） | ✅ |

## 五、使用

```bash
# 单股 (代码, 非名称)
python -m tools.cninfo.downloader --stock 300308 --years 2023,2024,2025
python -m tools.cninfo.downloader --stock 300308 --no-download   # 只落盘元数据
python -m tools.cninfo.downloader --stock 300308 --all-periodic  # 四类全下

# 批量 (无 --stock)
python -m tools.cninfo.downloader --limit 20                     # 试跑
python -m tools.cninfo.downloader --max-minutes 120              # 每晚 2h 自动续传
python -m tools.cninfo.downloader --index 000300.SH --with-pdf   # 指数成分+全文
python -m tools.cninfo.downloader --download-watchlist 300308,600519 --years 2024
python -m tools.cninfo.downloader --download-index 000300.SH --years 2024

# 解析 (AR/H1) — 用 .venv 的 python
.venv/Scripts/python.exe -m tools.cninfo.parse 300308 --year 2024 [--audit]
```

对话（放开写工具后）：`拉取中际旭创2024年年报` → `fetch_annual_report`（名称
解析在工具内完成）→ 返回 file_path → `extract_document` 解析全文提问。
