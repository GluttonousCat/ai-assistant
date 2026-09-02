# 研报中心设计文档

> Alpha Finance Radar 平台 · 知识星球研报库子系统
> 更新：2026-09-02

## 一、系统设计

### 1.1 数据流水线

```
知识星球 (每日 07:00/23:00 调度, core/zsxq_scheduler.py)
   │ 爬话题 + 逐个下载 PDF 附件 (反检测限速 60-120s/个)
   ▼
★ 逐篇流水 (scripts/daily_fetch_zxsq.py:_process_one_report,
   每下载完 1 个文件立即执行, 不等整批 —— 下载完即"已分析", 前端立即可用):
   1. 增量同步 PG (scripts/sync_zxsq_to_pg.py, 返回本次新入库 report_id)
      → fin.report_meta (本地文本层抽取正文, 有文本层即 content_chars>0)
   2. 对新入库的 PDF/docx 逐篇:
      ① LLM Analysis 轻量元数据 (tools/finance/report_meta_analysis.py,
         输入仅文件名 → title/org/target/industry/region/market, ~2s)
      ② 深度提取 (skills/report/skill.py 单篇 extract:
         正文 → 评级/盈利预测/核心观点/tags; A股 ts_code, 非A股 symbols)
      (话题 txt 条目跳过, 由 merge 归并进同话题 PDF, 不浪费 LLM)
   3. 去重合并 (scripts/merge_report_duplicates.py)

图片型 PDF (无文本层, content_chars=0): 深度提取时转后台 qwen 视觉 OCR
  (状态 pending_ocr, 前端隐藏) → OCR 完成 → ★自动接续单篇深度提取
  (不再等下一轮调度)

兜底 (调度器 _run_round 轮末, 12h 一轮): LLM Analysis 积压清理(40) +
深度提取批量限额(8/15 篇) —— 只兜漏网/存量, 主链路已是逐篇流水
```

### 1.2 表结构（fin.report_meta 关键列）

| 列 | 来源 | 说明 |
|----|------|------|
| `file_name` | 爬虫 | 原始文件名（永不变更，溯源用） |
| `title` | LLM Analysis | 清洗后标题（去机构前缀/中文版/译文/.pdf 后缀，只留内容主题） |
| `org_name` | LLM Analysis | 机构（高盛/伯恩斯坦/野村…） |
| `target` | LLM Analysis | 标的名——**仅具体公司或商品**；行业/宏观报告为 NULL（不硬凑） |
| `industry` | LLM Analysis | 行业（规范化词表：半导体/电子元件/贵金属/农业…） |
| `region` | LLM Analysis | 地区（中国/美国/日本/亚太/全球…） |
| `market` | LLM Analysis | A股/美股/HK股/宏观/商品/行业/其他 |
| `ts_code` | 深度提取 | A 股标的代码（个股研报才有） |
| `symbols` | 深度提取 | 非 A 股标的代码（NVDA.O / 0981.HK） |
| `analysis_json` | 深度提取 | 完整提取结果 JSON |

`fin.report_forecast`：盈利预测（report_id 关联，A 股用 ts_code、非 A 股用 symbol+market）。

### 1.3 去重合并规则（scripts/merge_report_duplicates.py）

同一知识星球话题常产生多条记录（PDF 原文 / txt 中文解读 / mp3 音频）：

1. 同 topic 保留 **PDF 条目**；PDF 无正文时把 txt 解读文本并入
2. txt/mp3 条目删除（88 条冗余已清理，128→40）
3. 无 PDF 附件的独立 txt 话题标记 `zsxq_topic_pdf_absent` 不展示

### 1.4 列表展示（Reports.jsx）

`标题 | 机构 | 标的 | 行业 | 地区 | 市场 | 发布日期 | 状态 | 操作`
未分析行操作列有「**AI 分析**」按钮（原「AI 提取」，2026-09 更名）：点击弹出
**AI 分析流式弹窗**（见 1.5），完成后行状态变「已分析」。
详情弹层：盈利预测表（AI 分析）+ 正文 + 原始文件名。
筛选项：搜索（标题/机构）+ 状态（已分析/未分析）+ 市场。

### 1.5 AI 分析流式弹窗（2026-09-02 新增）

**需求**：点击「AI 分析」即时弹窗，把后台 Agent 处理流程流式展示出来；
模型一旦开始产出内容，即在弹窗内逐块输出。

**后端链路**（api/reports_api.py + skills/report/skill.py）：

```
POST /api/reports/{id}/analyze/stream   (SSE, 需 JWT)
  ├─ ReportSkill.extract_stream(report_id)  同步生成器 (worker 线程执行)
  │    stage load    读取研报 / 正文就绪 (N 字)
  │    stage ocr     图片型 PDF → 提交后台视觉 OCR, 友好报错退出 (不标 failed)
  │    stage extract LLM 结构化提取中 (deepseek, stream + enable_thinking=False)
  │    delta ×N      LLM 原始 JSON 输出逐块 (弹窗内流式滚动)
  │    stage parse   JSON 解析 + 词表/代码规范化
  │    stage save    入库 report_meta + report_forecast (预测 N 条)
  │    data          结构化摘要 (market/rating/标的/core_view/key_points/forecasts)
  │    done
  └─ API 层: worker 线程 → queue 桥接 → async SSE 生成器
       (同步重 IO 不进事件循环; error 后必补发 done, 前端统一收尾)
```

事件协议与 Agent 问答（/api/v1/query/stream）同构：
`stage {stage,message} / delta {text} / data {data} / error {message} / done {}`。

提取与规范化逻辑由 `_apply_extract_result` 统一承载，
批量链路（`_run_extract`/`_extract_one`）与流式链路共用，行为一致。

**前端**（Reports.jsx + platformApi.js）：
- `platformApi.analyzeReportStream(id, onEvent)` 返回 `{promise, abort}`，
  SSE 解析复用 `sseStream` 助手（与 chatStream 共用）；
- 弹窗三区：**Agent 流程**时间线（阶段逐条点亮，当前步 spinner）→
  **模型输出·流式**（原始 JSON 逐块滚动 + 光标闪烁，贴底跟随）→
  **结构化结果**（市场/评级/标的徽章 + 核心观点 + 要点 + 盈利预测表）；
- 「后台运行并关闭」/关闭弹窗时 `abort()` 只断 SSE 连接，worker 线程照常完成入库；
- 完成（或出错）后自动刷新列表状态徽章。

**注意事项**：
- 关思考模式对流式同样必须：`stream(..., extra_body={"enable_thinking": False})`；
- 流式提取期间持有一条 PG 连接（约 1-2 分钟），与批量链路量级一致。

## 二、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 列表混杂大量 txt/mp3 且同研报重复 | 爬虫把话题文本/PDF/音频都入库 | 去重合并脚本（1.3）+ API 只展示 PDF 来源 | ✅ 已修复 |
| PDF 研报正文为空 | 图片型 PDF 文本提取为 0 字符 | 视觉 OCR 链路已实现（qwen-vl-max），默认未启用，后续开启 | ⏸ 暂缓 |
| 标题一长串机构前缀+文件名噪音 | 初版直接用原文件名 | LLM 清洗：去「中文版-机构-」前缀、「-译文」「.pdf」后缀 | ✅ 已修复 |
| 行业研报出现个股代码（如 005930.KS） | 深度提取把正文顺带提到的公司收进 symbols | prompt 明确「行业报告一律 []，正文提到的公司不收」+ 存量清理 | ✅ 已修复 |
| 标的列把行业词当标的（如「日本电子元件」抽成标的） | 抽取规则不严格 | prompt 强规则：仅具体公司/商品才算标的；行业报告给 NULL | ✅ 已修复 |
| 主题标签列多余 | 多轮迭代残留 | 本轮移除该列，标的列只认 `target` 字段 | ✅ 已修复 |
| 新研报未做初步分析（标题仍是文件名/机构地区空） | 公网访问者点「AI 提取」触发图片型 PDF 视觉 OCR（每册 2-5 分钟），同步阻塞提取线程，LLM Analysis 批次被长时间占死 | OCR 转后台线程（状态先置 `pending_ocr`，完成后自然接上）；批量提取不再被单册 OCR 卡住 | ✅ 已修复 |
| 市场列值混乱（出现「行业」「亚太」等非市场分类） | 提取 prompt 词表含「行业」且无强约束，模型把行业/地区填进 market | market 词表收敛为交易所分类：A股/H股/TW股/日股/韩股/美股/欧洲股/东南亚股/商品/宏观/其他；prompt 明示「禁止填行业名或地区名」；加旧值归一化映射（行业→其他、HK股→H股） | ✅ 已修复 |
| 地区值不规范（「中国」「亚洲」等） | 词表未强制 | region 词表规范化：中国大陆/HK/TW/日本/韩国/东南亚/印度/澳大利亚/美国/欧洲/亚太/全球；存量归一化 | ✅ 已修复 |
| OCR 未完成时前端展示空壳条目 | 视觉 OCR 转后台后 `pending_ocr` 状态未过滤 | 列表 API 排除 `extraction_status='pending_ocr'`（OCR 完成自动出现） | ✅ 已修复 |
| 分页只能靠上一页/下一页翻 | 无页码跳转控件 | pager 新增「跳至 N 页」输入框（Enter 或按钮触发，越界忽略）；CSS `.pager-jump` | ✅ 已修复 |
| 新账号登录 | — | 管理员账号 `Gluttonouscat`（admin 角色）；注册门槛保持 注册码 123456 | ✅ 已验证 |
| 点「AI 提取」报 pg is not defined | `_vision_backfill` 改后台线程后，状态更新误用了外层作用域的 `pg` 变量（staticmethod 内不可见） | 改为独立 `PgClient` 连接执行 `pending_ocr` 状态更新 | ✅ 已修复 |
| 新研报元数据整批缺失反复出现 | 上述两 bug 叠加：OCR 卡线程 + 状态机错乱，LLM Analysis 批次失效 | 修复后手动补提取 9 篇；后续由调度器自动处理 | ✅ 已修复 |
| 盈利预测从未写入 `fin.report_forecast`（详情页预测表靠旧数据撑着） | 预测入库代码块被误放进 `_display_title` 的 `return` 之后成死代码，`_extract_one` 只算不写 | 拆出 `_apply_extract_result` 共用入库逻辑（report_meta + report_forecast 同步写），删除死代码；流式/批量两条链路共用 | ✅ 已修复 (2026-09-02) |
| 图片型 PDF 被批量提取标 `analysis_status='failed'`，OCR 完成后永远不再被自动提取 | OCR 转后台时 `_run_extract` 无条件标 failed，而自动提取条件排除 failed | 仅「无源文件可 OCR」才标 failed；排队 OCR 的保持 none，下轮自动接上；`_vision_backfill` 对 `pending_ocr` 幂等防重复起线程 | ✅ 已修复 (2026-09-02) |
| 深度提取永远追不上下载，"未分析"研报越积越多（前端十几个不可用） | 深度提取只在**整轮爬完后**由调度器批量跑且限额 8/15 篇，而下载 15 篇/轮 × 2 轮/天；逐篇流水里只有 LLM Analysis 没有 AI 分析 | 深度提取搬进下载流水线：`sync.run()` 返回新入库 report_id（INSERT RETURNING）→ `_process_one_report` 对刚入库的 PDF/docx 逐篇「元数据+深度提取」，下载完即 done；调度器轮末批量保留为兜底 | ✅ 已修复 (2026-09-02) |
| 新研报元数据迟迟不提取（标题仍是文件名），被旧积压挡住 | 逐篇流水里的 `analyze_pending(limit=5)` 按 `report_id` **升序**取最旧 5 篇，有积压时新篇永远排不上 | 改为精准单篇 `analyze_report_meta(新report_id)`；`analyze_pending` 仅在无新文件时兜底清积压 | ✅ 已修复 (2026-09-02) |
| 图片型 PDF OCR 完成后最长 12h 无人接续深度提取 | OCR 后台线程只回写正文，提取要等下一轮调度（07:00/23:00）碰到它 | `_vision_backfill` 的 OCR 完成回调里直接跑单篇深度提取（此刻已有正文，不再进视觉分支，无递归） | ✅ 已修复 (2026-09-02) |
| 逐篇流水上线后前端仍见大量"未处理"（存量积压：元数据缺 128 + 深度提取 52 + 图片 PDF 90） | 流水线只管**新下载**；存量靠轮末兜底（元数据 40 篇/12h、深度提取 8-15 篇/12h）要清数天~数周 | 一次性回填脚本 `scripts/backfill_report_analysis.py`（元数据/深度提取/OCR 三阶段，均最新优先、幂等可重跑，与调度共存）；OCR 阶段串行防线程风暴 | ✅ 已修复 (2026-09-02) |
| OCR 失败/异常的 PDF 被每轮无限重试 OCR（白烧 qwen），或异常时状态卡 `pending_ocr` 前端永久隐藏 | `_vision_backfill` 失败只置 `extraction_status`，`analysis_status` 仍 none → 每轮重新选中再 OCR；ocr_pdf 抛异常时状态不更新 | 失败与异常统一置 `extraction_status='failed' + analysis_status='failed'`（终态，轮次不再自动选；「AI 分析」按钮仍可手动重试） | ✅ 已修复 (2026-09-02) |

## 三、LLM Analysis Prompt 要点（现行版本）

```
你是研报文件名清洗与元数据提取引擎。只输出 JSON:
{title, org, target, industry, region, market}

- title: 去掉开头机构名及分隔符(伯恩斯坦-/【高盛】/中文版-高盛-)、
  去掉尾部噪音(-译文/.pdf/.mp3); 只删减不改写
- target: 必须是具体公司名(常带代码)或商品名; 行业综述/宏观给 null;
  不要把行业词(如"日本电子元件")当标的
- industry: 从词表选 1 个(半导体/电子元件/贵金属/…)
- region: 从词表选 1 个(中国/美国/日本/亚太/全球/…)
- market: A股/美股/HK股/宏观/商品/行业/其他
```

实现细节：deepseek-v4-flash 为思考模型，必须 `extra_body={"enable_thinking": False}`，
否则思考吃光 token 且 `content` 为空（曾导致整批「解析失败」，已修复）。

## 四、AI 分析流式弹窗验证记录（2026-09-02）

| 验证项 | 方式 | 结果 |
|--------|------|------|
| extract_stream 事件流 (正常链路) | `scripts/test_report_stream_mock.py` mock PG/LLM | stage→delta×14→parse→save→data→done 全序正确；delta 拼接=原始 JSON；`report_forecast` DELETE+INSERT 均执行；analysis_status='done' |
| 图片型 PDF 分支 | 同上 (content_chars=0 + file_path) | stage ocr + error 友好提示；**不**写 analysis_status='failed' |
| enable_thinking 关闭 | mock 断言 invoke/stream 均传 `extra_body` | 通过 |
| SSE 端点鉴权/协议 | TestClient 直连 app (不起 lifespan) | 无 token 401；带 token 200 + `text/event-stream`，事件 `stage/error/done` 序列正确 (error 后补 done) |
| 前端 | `npm run build` (vite) | 构建通过，dist 已更新 |
| 逐篇下载流水线 (本次: 深度提取进流水) | `scripts/test_fetch_pipeline_mock.py` | 新入库仅 PDF 逐篇 元数据+深度提取（txt 跳过）；无新文件 noop+兜底；返回 `ok+extracted 1` |
| sync.run() 返回新 report_id | 真实 PG 增量空跑 (幂等) | 返回 `[]` 无错；INSERT 改 `RETURNING report_id` 不影响存量行为 |
| 存量积压回填 | `scripts/backfill_report_analysis.py` 真实运行 3 轮 (日志 logs/backfill_report_analysis.log) | 服务补跑轮 bulk-sync 148 个历史下载文件带来二次积压，脚本幂等重跑吸收；最终：元数据缺失 128→1、深度提取 52→0、截图行全部 done；剩 39 册图片 PDF 由脚本+调度并行 OCR（约 1-2h） |
| 生产链路 | 待线上点按「AI 分析」实测 (需重启常驻服务加载新代码) | ⏳ |

> 注意：常驻服务（计划任务 `AIAssistantServer`）不会热加载，后端改动需重启后生效；
> 前端 `web/dist` 已重新构建，重启后同批生效。
