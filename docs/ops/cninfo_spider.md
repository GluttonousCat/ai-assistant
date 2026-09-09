# 巨潮资讯爬虫（定期报告 PDF）设计文档

> Alpha Finance Radar · 数据获取子系统
> 更新：2026-09-08

## 一、系统设计

### 1.1 定位

从巨潮资讯网（cninfo，官方指定信息披露平台）拉取上市公司定期报告原文
（年报/半年报/一/三季报 PDF），入库元数据并下载文件。
用途：**年报交叉核验**（报告原文 vs Tushare 财务数据）与全文问答
（配合 `extract_document`）。

与知识星球爬虫（`tools/zsxq`）的区别：巨潮**按需拉取**（指定股票+年度），
不做每日调度——定期报告一年就几份，调度无意义。

### 1.2 模块结构（分层清晰，无横向依赖）

```
tools/cninfo/
  client.py      纯 HTTP 层: orgId 解析 / 公告检索(翻页) / PDF 下载
                 (直连 trust_env=False 绕系统代理 + 随机延迟 + 指数退避 + %PDF 魔数校验)
  filters.py     筛选纯函数: 类别识别 / 报告年度解析 / 干扰版排除 (摘要/英文/更正)
                 (顺序敏感: 半年度报告 含子串 年度报告, 短类别先匹配)
  downloader.py  编排 + CLI: 股票解析→查询→筛选→元数据入库→下载 (存在性幂等)
  tests/         筛选纯函数单测 (无网络)
```

```
storage/pg_schema.py   DDL_CNINFO_ANNOUNCEMENT (fin.cninfo_announcement, 集中管理)
mcp/tools/reports.py   fetch_annual_report 工具 (read_only=False, 沙盒默认排除)
output/cninfo/downloads/<ts_code>/<year>_<category>.pdf
```

### 1.3 端点与关键参数（实测 2026-09）

| 端点 | 用途 | 要点 |
|------|------|------|
| POST `/new/information/topSearch/query` | 代码→orgId | 沪市 `gssh0+code`（可推导），深市 `9900xxxx`（必须查询） |
| POST `/new/hisAnnouncement/query` | 公告检索 | `column=szse` **沪深通用**；`category_ndbg_szsh` 等类别参数；**必须带 orgId**（实测只传 code 返回 0） |
| GET `static.cninfo.com.cn/<adjunctUrl>` | PDF 下载 | 校验 `%PDF` 魔数防错误页 |

### 1.4 关键设计

- **orgId 三级策略**：内存缓存 → topSearch 查询 → 沪市规则推导兜底
  （topSearch 端点限流最敏感，实测高频调用 504）
- **披露窗口跨年**：N 年度报告在 N+1 年披露，查询窗口取
  `N-01-01 ~ N+1-12-31` 宽窗，再按标题解析的报告年度精确归属
- **幂等**：元数据按 `announcement_id`（巨潮原生）主键 upsert；
  下载按「file_path 已回填且文件在盘」存在性跳过（非水位线——见 AGENTS.md 踩坑表）
- **全文只挑一份**：全部公告（含摘要等）入库留痕，下载仅取筛选器选出的正式全文
  （类别优先 年报>半年报>季报，同类取最新）；更正/更新版本默认排除，元数据可追溯

## 二、需求优化

### 2.1 全量批量（2026-09-08 增补）：元数据全量 + PDF 按需

**量级实测账**（2026-09）：全市场 ~5400 只 × 5 年 × 4 类 ≈ **10 万条公告**。

| 层 | 方案 | 量级 | 说明 |
|----|------|------|------|
| PG 元数据 | `fin.cninfo_announcement` 全量入库 | ~10 万行 | PG 毫无压力；披露事件源（财报季工作流直接用 announce_date，不用下载一个 PDF） |
| PG 进度 | `fin.cninfo_sync_state`（ts_code 主键） | ~5400 行 | 按股票断点续传；股票枚举天然有序，**非**"下载完成顺序≠ID顺序"的水位线坑 |
| 文件 | `output/cninfo/downloads/` **按需** | 关注池 GB 级 | PDF 全量 ≈ **300GB**，本机磁盘放不下（余 88GB）；且 10 万次全文 LLM 解析的消费端不成立 — 关注池批量 / 对话内单只拉取 |

- 查询优化：每类别一次宽窗查询（5 年 4 类 = **4 请求/股**，非 20 次；
  分号多类别参数实测不可用）
- 限流对策：股票间 2~5s 随机延迟、指数退避重试、**连续 8 只失败熔断优雅停**、
  `--max-minutes` 时间预算分夜跑；沪市 orgId 可规则推导（`gssh0+代码`）绕过
  最脆弱的 topSearch 端点，深市必须查询
- 预估：健康限流下 ~15-20s/股，全量元数据 **约 20~30 小时**，建议每晚
  `--max-minutes 120` 分夜跑（进度自动续传）

```bash
python -m tools.cninfo.batch --limit 20                        # 试跑
python -m tools.cninfo.batch --max-minutes 120                 # 每晚 2h, 自动续传
python -m tools.cninfo.batch --download-watchlist 300308,600519 --years 2024  # 关注池 PDF
```

### 2.2 按需单只

- MCP 工具化后 Agent 可直接说"拉一下 XX 年报"；写类副作用工具，
  默认被对话沙盒排除（`openai_tools(include_write=False)`），需要时显式放开

## 三、问题（历史问题与处理记录）

| 问题 | 根因 | 处理 |
|------|------|------|
| 系统代理下请求超时 | 走了本机代理（127.0.0.1:10808），巨潮国内站直连才通 | `session.trust_env=False` 绕代理 |
| topSearch 频繁 504 Gateway Time-out | 端点限流敏感，短时间多次调用即触发 | orgId 内存缓存 + 沪市规则推导兜底 + 指数退避（3/12/27/30s，4 次重试） |
| 「半年度报告」被误分类为年报 | 筛选词序问题："半**年度报告**"含子串"年度报告" | 类别匹配顺序改为 季报/半年报在前、年报最后 + 单测防回归 |
| 无 orgId 查询返回 0 | hisAnnouncement 必须带 orgId | orgId 成为必经路径，三级策略保障 |
| pg_schema NameError | DDL 常量定义在 FIN_ALL_DDL 引用之后（Python 自上而下执行） | 定义移到列表之前 |

## 四、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-08 | 批量爬取试跑：limit 5 双轮（第二轮 skip=4 断点续传生效，失败股自动补跑），累计 9 只 done / 163 条元数据；504 限流下 4/5 成功，熔断与退避工作正常 | ✅ |
| 2026-09-08 | E2E：中际旭创+贵州茅台 2024 年报 查询→入库→下载（6.9MB/3.6MB，%PDF 校验过）；幂等重跑 downloaded=0 skipped=2 | ✅ |
| 2026-09-08 | MCP 工具 fetch_annual_report：实测含 504→重试成功→幂等跳过全程 31.8s，返回元数据+file_path | ✅ |
| 2026-09-08 | pytest：tools/cninfo/tests + mcp/tests + agent/tests 共 53 passed | ✅ |

## 五、使用

```bash
# CLI (项目根目录)
python -m tools.cninfo.downloader --stock 中际旭创 --years 2023,2024,2025
python -m tools.cninfo.downloader --stock 300308,600519 --no-download   # 只入库元数据
python -m tools.cninfo.downloader --stock 中际旭创 --all-periodic       # 含半年报/季报
```

对话（放开写工具后）：`拉取中际旭创2024年年报` → `fetch_annual_report` →
返回 file_path → `extract_document` 解析全文提问（年报交叉核验工作流的底座）。
