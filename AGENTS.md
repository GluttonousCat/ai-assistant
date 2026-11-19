# AGENTS.md — Alpha Finance Radar 项目上下文入口

> 本文件是给 AI Agent（及新成员）的**项目第一入口**。读完本文件即可建立全局认知，
> 再按需深入 `docs/` 对应模块文档。人类同事请从 [docs/README.md](docs/README.md) 进。
> 更新：2026-11-19

## 1. 项目是什么

**Alpha Finance Radar**（`ai-assistant`）——运行在 Windows 上的个人智能投研平台。

三大数据/能力支柱：
1. **Tushare A 股数据**：日线行情 + 财务三表 + 估值，PG 数仓（`stock`/`fin` schema）
2. **知识星球研报**：爬虫每日抓取 → LLM 解读入库（券商研报结构化：机构/标的/评级/盈利预测）
3. **Agent 问答**：自然语言 → 意图路由 → Text-to-SQL 查询 / 研报解读，SSE 流式输出


## 2. 目录与分层契约（关键 30 秒）

```
harness/     prompts(数据口径)/execution(工具沙盒)/loop(决策入口)；引擎层 (Agent Harness — 模型之外的一圈机器, 借鉴 DSH/Cordis 范式):
             loop(三级漏斗决策循环)/subagent(五域子代理)/intent(分层意图)/
             session(会话=历史+股票槽位)/trajectory(全量轨迹JSONL)/llm.py(模型路由工厂)/
             mcp(工具协议层: 33工具ToolSpec+stdio server, 外部客户端 python -m harness.mcp.server)
             (ctx 事件总线/插件化: 已有设计, 按 YAGNI 未落地)
core/        基础设施 (引擎与各层共用的地基, 不含业务):
             config(.env+yaml)/logger(按顶层包分桶)/security(JWT+bcrypt)/
             net/helpers·paths/json_response(共用模型JSON解析)
             config并发初始化完整发布/失败可重试；logger并发只建一组handler；security仅密码/JWT，临时密钥每进程固定
skills/      领域技能 (每技能一目录, 专业知识放 prompt):
             quant新增candidate_quality(原始质量与月度排序分分离/72原始分门槛)、label_research(冻结因果特征恢复/辅助标签比较/绝对与相对收益确认)、wyckoff(量价事件适配及移动防守)、wyckoff_hybrid(Spring反转与均线量价回调/按机会类型管理及反转席位限制，均限研究)、support_protection(买入后已确认60F更高低点/盘后上移保护)、stop_orders(显式预挂保护/完成60F后下一根才启用保本/标注日K替代，不改变默认盘后执行)；
             quant(workspace自由组合规则/自然语言条件树/模板兼容/冻结任务；rule_models·rule_indicators·rule_prepare负责有界规则/多周期因果计算，RuleExecutionConfig实际买价止损/显式离场；workspace_runner分派规则或v8模板；v10 tranches/tranche_engine/tranche_prepare:442/532分批、逐批T+1/FIFO、完整周期胜率及部分已实现盈亏；v9结构持有/固定对照；v8参数研究/盘后触发次日执行; 见 docs/agents/quant_backtesting.md) /
             trend(episodes.py连续入选合轮/升级不重开/20交易日归档；chart_history.py独立5年日K展示不改冻结信号；tracking.py历史名单前向跟踪/T+1·3·5·10/真实因子/交易日/缺失分离；parent_context.py独立日K母趋势标注，局部60F反转可属上涨回调，保留原信号与入场条件；structure.py V9有序结构；swing.py V8底背离/回调；V7及更早快照兼容; M8/M13/M21; 见 docs/frontend/stock_dashboard.md) /
             fin_query(Text-to-SQL) / report(skill无SQL编排/normalization纯规范化/ocr后台队列/analysis短事务及提交后续接/collection兼容出口) / scanned_report(OCR) /
             beta_alpha(产业链Beta+个股Alpha: chains种子链/analysis；回归在tests/skills/beta_alpha) /
             content(内容资产管线: 画像/公众号文章/PPT/年报解读) /
             research(研究生产线: 证据→十章草稿→成稿, 任务分派见 docs/research/tasks/)
workflows/   常驻服务与数据流程，按职责分包
             runtime/：lifecycle 组合依赖、operations 注入后台接口、scheduled_task 统一启停，另有独立哨兵和企微桥接
             market/：daily_scheduler 交易日17点增量与失败重试；intraday_scheduler 四轮收盘120秒后采集；
                     refresh/collect_intraday/scan_intraday/scan_trend 负责回补、逐股审计与原子发布；
                     digest_scheduler 调度 11:55 午间和18:00盘后两类邮件，分别保留回执
             reports/：scheduler 每日00:00及12:00触发 fetch，逐篇通过 sync 和 deduplicate 入库及去重
             quant/：worker 持久队列、OS锁串行、独立回测进程、默认15分钟超时
jobs/        冻结研究、回测和自检命令。已发布研究按历史源文件指纹续跑，保留原始路径。
             旧版 jobs 模块及 python -m 调用通过兼容入口转到对应 workflows 模块。
             各版本实验口径、样本区间和验证结果见 docs/agents/quant_backtesting.md
tests/       Python 回归按 api、core、harness/mcp、workflows、skills、tools、storage 分组；
             test_boundaries.py 检查生产代码依赖，真实数据库用例标记 integration
tools/       pdf(通用PDF工具: 文本/渲染/OCR/表格) / zsxq爬虫 / market行情同步 /
             cninfo巨潮定期报告(获取+解析, 自包含) / langgraph(LangGraph原子封装: 建图/记忆) / finance(SQL guard, KB, 研报文件提取器)
             auth/users(账户服务与空表种子)；finance/data·report_text·report_analysis·industry_data·vector_collections(具体数据能力出口，不暴露PG连接给MCP/领域层)
storage/     pg.py(连接池) / sqlite(爬虫) / schema/(领域DDL) / repositories/(仓储SQL)
             vector_chunks(共用向量集合生命周期/并发首次创建合并)；报告/年报字段与产业链DDL归schema
             pg_schema.py保留旧导入兼容；新增DDL按域归档，独立连接部署
api/         路由+鉴权中间件(JWT) / agent_api(SSE) / chains_api / schema_api / market_api / reports / auth
             finance.py仅保留兼容路由聚合；events.py统一SSE编码
web/src/     Login / Platform(侧边栏壳+角色门控) / AgentChat(智能对话,SSE) / ChainView(行业挖掘页)
             lib/http(JSON/SSE/下载共用鉴权与错误)·polling(串行刷新/取消/可见性)/hooks/useSession/components/FeatureBoundary；页面与走势图按需加载
             chat/resultData(纯图形数据)/ResultLineChart(绘图)，旧platformApi出口兼容
             market/(StockDashboard+TrendChart+SignalTracking+IntradayWatch,股票行情#market含趋势筛选/信号跟踪/独立盘中观察；四轮自动更新/可拖拽60F与5年日K，按轮次隔离截止) /
             quant/(StrategyWorkspace规则工作台+QuantDashboard历史报告+EquityChart,策略与回测#quant) /
             Reports / UserAdmin / Monitor(后台任务/日志/Agent观测SSE, adminOnly); Markdownish 共享渲染器；
             opportunities.js/OpportunityBadge.jsx统一趋势回调（蓝↑）与底部反转（紫■）展示，不修改历史信号
```

**分层契约**：`api → harness(引擎)+skills(领域) → tools → storage`，
core 是全层共用的基础设施，harness.mcp 是对外的工具协议出口，只允许上层调下层：

- **api**：HTTP 入口，鉴权（middleware 白名单外全 JWT）、参数校验；重逻辑不写在这
- **harness**：引擎——LLM 决策循环、意图路由、工具协议(mcp)；不直接摸 storage
- **skills**：领域方法论，编排 tools + LLM prompt
- **tools**：原子能力，无业务状态；新视觉/提取能力进 tools/pdf（PDF 文本/渲染/OCR/表格）
- **storage**：唯一碰 PG/SQLite 的层；DDL 集中在 `storage/schema/`/各模块 ensure 函数，`pg_schema.py`仅兼容旧导入

**写代码前先看**：`core/config.py`（配置优先级 env > yaml）、`api/deps.py`（require_admin 是
DB 实时角色校验）、`storage/pg.py`（PgClient 是上下文管理器，事务自动提交/回滚）。

## 3. 数据流水线（研报链路，最常改动）

```
00:00/12:00 调度 → 爬话题 + 逐个下载PDF(反检测,60-120s/个)
  ※ 中文翻译版(文件名"中文版-"等, is_chinese_translated)下载/入库层直接过滤, 保留英文原版
  → 每下载1个立即(逐篇流水, 下载完即"已分析"前端可用):
     sync_zxsq_to_pg 入库(返回新report_id) → LLM Analysis(仅文件名→标题/机构/标的/行业/地区/市场)
     → 深度提取(正文→材料性质/判断/假设/预期变化/跟踪条件/评级/预测，原文引述保留原语言)
     → 去重按附件身份判定，同话题可保留多份原件，话题文字独立留存；存量分批升级见 docs/frontend/reports.md
  ※ 深度提取也可由研报中心「AI 分析」按钮 SSE 流式触发(弹窗展示 Agent 流程+模型输出,
    POST /api/reports/{id}/analyze/stream; 与批量链路共用 _apply_extract_result)
  ※ 轮末批量提取(限额8/15篇)与 analyze_pending 仅作兜底清积压, 不在主链路上
图片型PDF(无文本层) → 后台线程 qwen 视觉OCR(状态 pending_ocr, 前端隐藏)
  → OCR完成后自动接续单篇深度提取(不等下一轮调度)
```
关键表：`fin.report_meta`（file_name 原名/title 清洗后/org/target/industry/region/market）、
`fin.report_forecast`（盈利预测）。market 词表：A股/H股/TW股/日股/韩股/美股/欧洲股/东南亚股/商品/宏观/其他。

另：**年报披露项提取**（tools/cninfo/parse.py，自包含解析、纯文件产物不碰 SQL；
2026-10 从 skills/content/annual_items.py 迁入 cninfo 模块，输入只认代码）：
cninfo 报告 PDF → MD（output/cninfo/md/）→ `{items, sections}` JSON。
items=第二章精确两项（kpi3y/nonrecurring，词表+相似度兜底，分季度走 Tushare SQL）；
sections=三~七节按 L1 小节全量（mdna 全量/治理·事项关键词/股东整节+节首前文/
债券<500字跳过；结构不符的节跳过并记审计，`--audit` 看命中方式与跳过原因）→
`output/cninfo/md/<代码>/<年度>_<类别>_items.json`。节定位双方言+混合式
（`第X节/章`锚≥4 / 主题式动态选节级），详见
[docs/agents/profile_extraction_handoff.md](docs/agents/profile_extraction_handoff.md)。

## 4. 模型路由（改模型只动 config.yaml）

`config.yaml llm.models.<用途>`：default/agent/extract/analysis=deepseek-v4-flash-0731，
vision=qwen3.8-flash（deepseek 不收图片）。**思考开关由 `llm.enable_thinking` 全局控制
（当前 true=最强思考；vision 用途已覆盖关闭）；`llm.max_tokens` 必须给足（32768，思考与
正文共用预算）——两者由 `harness/llm.py` 统一注入，业务代码禁止传 `enable_thinking`**。
代码用 `get_agent_llm()/get_extract_llm()/get_vision_llm()` 工厂，禁止硬编码模型名。详见
[docs/agents/llm_models.md](docs/agents/llm_models.md)。

## 5. 认证与角色（改权限先读）

- 全 API 需 JWT（登录/注册/health 白名单）；注册需邀请码（.env `AUTH_INVITE_CODE`）
- 三级角色：超级管理员（.env `SUPER_ADMIN`，用户管理独占）/ admin（实时监控）/ user
- `require_admin` 查 DB 实时角色（不信 token 内 role）——提权/降权即时生效
- `/api/auth/users*` 整组仅超级管理员
- 种子管理员仅当用户表为空时创建（防删除后重启复活）

## 6. 已踩过的坑（新代码必读，防复发）

| 坑 | 规则 |
|----|------|
| deepseek 思考吃光 token（content 空） | 根因是 **max_tokens 太小**而非思考本身：开思考必须配足 `llm.max_tokens`（32768）；思考开关 config 统一控制，业务代码禁止硬编码 |
| DDL 在事务内锁死整表 | `_ensure_columns` 用独立连接+缺才 ALTER（曾致全站卡死） |
| 孤儿控制台 stdout 阻塞事件循环 | 服务输出必须重定向文件，bat 用 tail 看 |
| 同步重 IO 在 async 上下文 | 用 `asyncio.to_thread`（21:00 同步曾卡死站点） |
| 水位线同步漏数据 | 下载完成顺序≠file_id 顺序，同步用存在性检查而非纯水位线 |
| bat 中文断句 | bat 内容纯 ASCII；延时用 `ping` 不用 `timeout` |
| LLM 提取词表漂移 | market/region 必须词表约束+旧值归一化映射 |
| LLM 输出语言漂移（英文原版→英文输出） | prompt 显式约束「所有文本字段一律简体中文，原文英文必须翻译」 |
| 中文版研报与原版重复 | 下载/入库层过滤 `is_chinese_translated`（保留英文原版，无原版时中文版顶上） |
| 地区敏感词 | 文档一律用代号 **TW / HK**（如 TW股/HK股），不用全称 |
| zip 对齐错配 | 多源数据按键(dict)匹配，绝不按位置 zip（range_trading 回测曾全错） |
| 走平均线换复权基准后信号翻转 | 斜率/均线贴合比较使用按ATR缩放的微小数值容差；覆盖前复权展示与首因子回测基准的一致性，不能把1ULP误差当趋势 |
| 回测原始价被复权计算改写 | `to_numpy`可能返回视图；原始执行价必须`copy=True`，与复权指标数组分离；撮合前校验raw×scale=adjusted，拒绝混合单位；旧失真批次保留并标记替代 |
| LLM prompt 花括号 | `.format()` 模板里的 JSON 花括号必须转义 `{{}}`（曾致 KeyError） |
| 视觉 OCR 阻塞请求线程 | OCR 一律后台线程 + 状态 pending_ocr（前端隐藏，完成后自动接续） |
| PG 服务端重启杀数小时批任务 | 长批任务按工作单元（每股）捕获 OperationalError 重连续跑（batch.py `_process_stock`/sync_holders 模式）；注意 PgClient 连接在 `__enter__` 才从池获取，手动管理必须 `PgClient().__enter__()` 成对使用，裸构造 conn/cur 全是 None |

## 7. 开发规范（提交前自查）

1. **加表/加列**：DDL 写进 `storage/schema/` 对应领域模块或模块内 ensure 函数；用 `ADD COLUMN IF NOT EXISTS`；
   独立连接执行，绝不放在业务事务里
2. **加 API**：想清楚挂在哪个 router；除登录/注册外自动被鉴权中间件覆盖；管理类加
   `require_admin`；重逻辑 `asyncio.to_thread`
3. **加 LLM 调用**：用 `harness/llm.py` 工厂（purpose 路由），思考/max_tokens 由 client
   统一注入（业务层不传）；JSON 输出的 prompt 用 `{{}}` 转义；解析用宽容提取（剥 ```json 块）；
   面向用户的输出在 prompt 里显式要求中文
4. **加前端页**：`web/src/` 下新组件 + `Platform.jsx` NAV 注册（注意 adminOnly/superOnly）；
   改完必须 `npm run build`（后端托管 dist，不构建不生效）
5. **验证**：默认 `python -m pytest` 为离线回归；真实DB用例显式 `python -m pytest -m integration`，禁止通过按函数名排除隐藏依赖。改研报链路后跑 `python -m tools.finance.report_meta_analysis` 单篇验证；
   改 beta_alpha 跑 `pytest tests/skills/beta_alpha`
   （改链模板另跑 `python -m skills.beta_alpha.analysis.chain_analysis <chain_id>`）；
   改完 git commit（消息带日期与模块）
6. **分层验证**：提交前运行 `python -m jobs.check_boundaries` 与 `pytest tests/test_boundaries.py`。
   禁止新增跨层依赖；历史债务精确登记于 `docs/ops/dependency_baseline.json`，迁移后减少基线，不能整目录豁免。
   新API/Agent通过tools访问仓储，禁止直接写SQL或在HTTP请求里拉取外部分钟行情。
7. **日志**：一律 `get_logger(__name__)`（core/logger 按顶层包聚合落
   logs/agent.log / tools.log 等；禁止裸 `logging.getLogger` 与库代码 print；
   CLI 最终汇总/报表输出可保留 print）；级别/保留天数在 config.yaml `logging` 段
8. **临时文件**：pip/pytest运行前检查当前进程TMPDIR/TEMP/TMP，若指向项目根，先创建`output/tmp`，三者均指向其绝对路径再执行（TMPDIR优先）；
   用`tempfile.gettempdir()`确认生效，不能假定系统Temp可写。只改本次进程环境，不改用户系统设置；安装缓存/解包目录不能放根目录或提交。

## 8. 文档地图（深入阅读）

| 模块 | 文档 | 一句话 |
|------|------|--------|
| 前端登录 | [docs/frontend/login.md](docs/frontend/login.md) | JWT/邀请码/三级角色/校验规则 |
| 前端问答 | [docs/frontend/chat.md](docs/frontend/chat.md) | SSE 协议/意图路由 |
| 股票看板与趋势筛票 | [docs/frontend/stock_dashboard.md](docs/frontend/stock_dashboard.md) | 60F/日K趋势回调与阴跌砸坑反弹/独立截止/全池快照/可解释条件；不宣称已验证收益 |
| 量化策略与回测 | [docs/agents/quant_backtesting.md](docs/agents/quant_backtesting.md) | 注册策略/真实限价/T+1资金组合/2026样本内研究/固定名单偏差与实际已知名单对照 |
| 前端研报 | [docs/frontend/reports.md](docs/frontend/reports.md) | 流水线/表结构/去重/LLM Analysis/AI分析流式弹窗 |
| 运行观测 | [docs/frontend/monitor.md](docs/frontend/monitor.md) | 后台采集阶段/跨进程任务历史/日志筛选脱敏/Agent监控，仅管理员 |
| 前端框架 | [docs/frontend/framework_theme.md](docs/frontend/framework_theme.md) | 品牌/深黑暗棕主题/桌面入口 |
| 模型体系 | [docs/agents/llm_models.md](docs/agents/llm_models.md) | 用途路由/两个模型的坑 |
| 扫描件 Agent | [docs/agents/scanned_report_agent.md](docs/agents/scanned_report_agent.md) | PDF→PNG→视觉OCR 解耦设计 |
| MCP 工具层 | [harness/mcp/README.md](harness/mcp/README.md) | 33 工具(30 只读)/ToolSpec 规范/中文description约定/stdio 服务(默认只读) |
| 企微机器人桥接 | [docs/agents/wecom_bot.md](docs/agents/wecom_bot.md) | wss外拨收消息/流式回复/会话映射; python -m workflows.runtime.wecom_bot |
| Agent 对话循环 | [docs/agents/agent_loop.md](docs/agents/agent_loop.md) | LLM 自主决策+工具循环/多轮追问/累计口径知识/沙盒；新入口 `/api/v1/agent/stream`；沙盒参数与五域定义在 config.yaml `agent:` 段，全量轨迹落 logs/trajectories/ |
| dsh 客户端接入 | [docs/agents/dsh_client.md](docs/agents/dsh_client.md) | 外部 harness(dsh/Claude Desktop)连 MCP server；stdio 握手冒烟 jobs/smoke_mcp_stdio.py |
| 研究生产线 | [docs/research/tasks/T00_总纲_契约与分工.md](docs/research/tasks/T00_总纲_契约与分工.md) | 三Agent流水线(证据→草稿→成稿)/并行任务分派/契约 skills/research/contracts.py；方法论源 docs/research/company_industry_guide.md |
| 内容资产管线 | [docs/agents/content_pipeline.md](docs/agents/content_pipeline.md) | 上市公司画像/公众号文章/pptgen出片；`python -m skills.content` |
| 年报存储设计 | [docs/agents/annual_storage.md](docs/agents/annual_storage.md) | PG真相源+Milvus annual_chunks/锚点/单写路/双路召回；定稿待实现 |
| Beta/Alpha Skill | [docs/agents/chain_beta_skill.md](docs/agents/chain_beta_skill.md) | 产业链种子链/三源映射/环节指数/预期差四象限；Agent 会话入口在 `.agents/skills/beta-skill`、`.agents/skills/alpha-skill` |
| 安全 | [docs/ops/security.md](docs/ops/security.md) | 分层防护/事件审计/加固清单 |
| 结构优化方案 | [docs/ops/module_boundaries.md](docs/ops/module_boundaries.md) | 模块化单体/仓储收敛/DDL与Agent拆分/旧入口兼容；DDL/路由/执行/认证及财务查询已首轮收敛；遗留依赖登记并阻止新增 |
| 行情与新闻选型 | [docs/ops/data_sources.md](docs/ops/data_sources.md) | 五组指数成分池/免费盘后分钟与付费实时/新闻快照；含价格/探测记录；2026年60F五指数并集已入库，新闻待接入 |
| 每日图文邮件 | [docs/ops/market_digest.md](docs/ops/market_digest.md) | 后端11:55午间跟踪与新观察/18点正式日报/独立回执与去重 |
| 日线定时采集 | [docs/ops/daily_sync.md](docs/ops/daily_sync.md) | 交易日上海时间17:00/日K→60F→看板/启动补漏/5交易日重叠回补/逐股审计与失败重试 |
| 数据处理 | [docs/ops/data_process.md](docs/ops/data_process.md) | 历史：同步全流程 |
| 爬虫 | [docs/ops/spider.md](docs/ops/spider.md) | 历史：反检测设计 |
| 产品全貌 | [docs/PRD.md](docs/PRD.md) | v1.0 需求与架构（部分过时，以本文件为准） |

## 9. 文档维护约定（写文档前读）

1. 一模块一文档，先归档进 docs/ 对应子目录，并更新 README 索引
2. 统一五段结构：**系统设计 → 需求优化 → 问题 → 处理方案 → 验证记录**
3. Bug 修复必须留痕（根因+方案写入该模块「历史问题与处理记录」表）
4. 与代码实态冲突时以代码为准，并顺手修正文档
5. 地区名词用代号 TW/HK；bat/配置文件避免中文（编码坑）
6. 公开仓库不写部署细节与访问地址（内部运维文档除外且不含真实地址）
