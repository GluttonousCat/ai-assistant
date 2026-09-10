# AGENTS.md — Alpha Finance Radar 项目上下文入口

> 本文件是给 AI Agent（及新成员）的**项目第一入口**。读完本文件即可建立全局认知，
> 再按需深入 `docs/` 对应模块文档。人类同事请从 [docs/README.md](docs/README.md) 进。
> 更新：2026-09-02

## 1. 项目是什么

**Alpha Finance Radar**（`ai-assistant`）——运行在 Windows 上的个人智能投研平台。

三大数据/能力支柱：
1. **Tushare A 股数据**：日线行情 + 财务三表 + 估值，PG 数仓（`stock`/`fin` schema）
2. **知识星球研报**：爬虫每日抓取 → LLM 解读入库（券商研报结构化：机构/标的/评级/盈利预测）
3. **Agent 问答**：自然语言 → 意图路由 → Text-to-SQL 查询 / 研报解读，SSE 流式输出

另含独立量化子系统 `range_trading/`（震荡区间+趋势双系统全市场扫描，见 docs/quant/）。

## 2. 目录与分层契约（关键 30 秒）

```
agent/      智能体域 (一个顶层目录, 解耦语义保留在二级子包):
            loop/fin_graph/subagent  意图路由 + LLM自主决策工具循环 + 六域子代理
            skills/                  fin_query(Text-to-SQL) / report(研报) / scanned_report(OCR)
            beta_alpha/              产业链Beta+个股Alpha (chains种子链/analysis映射与指数/
                                     streaming SSE/schema DDL/tests; 对话页两条链路全在这)
            content/                 内容资产管线 (画像→公众号文章/PPT): profile十板块/
                                     charts五图/article六模块/annual_report年报解读
mcp/        MCP工具协议层 (21工具ToolSpec, stdio server; 外部客户端 python -m mcp.server,
            与 agent/loop 函数桥共用一份定义; 保持顶层=对外边界, 勿挪动)
core/       config(.env+yaml) / logger / security(JWT+bcrypt) / scheduler(Tushare 21:00)
            zsxq_scheduler(爬虫 07:00/23:00) / lifespan / llm(模型路由工厂) / helpers·paths
.agents/skills/ ZCode技能模块 (beta-skill/alpha-skill/interpret-skill: SKILL.md 会话内直接调用)
tools/      zsxq爬虫 / market行情同步(sync_mainbz主营构成, sync_holders十大股东) /
            cninfo巨潮定期报告 / finance(SQL guard, KB, pdf_vision视觉OCR)
storage/    pg.py(连接池) / sqlite(爬虫) / pg_schema.py(全部DDL,改表先看这)
api/        路由+鉴权中间件(JWT) / ws / finance(SSE流式) / reports / auth
web/src/    Login / Platform(侧边栏壳+角色门控) / AgentChat(SSE) / ChainView(产业链页)
            Reports / UserAdmin; Markdownish 共享渲染器
scripts/    管线入口脚本(daily_fetch_zxsq, sync_zxsq_to_pg, merge_report_duplicates, smoke)
range_trading/  量化子系统(特征/regime状态机/扫描/回测, 自带tests)
```

**分层契约**：`api → agent(内含 skills/beta_alpha/content) → tools → storage`，
mcp 是与 agent 平行的工具协议出口，只允许上层调下层：

- **api**：HTTP/WS 入口，鉴权（middleware 白名单外全 JWT）、参数校验；重逻辑不写在这
- **agent**：LangGraph 图，意图识别 + 路由；不直接摸 storage
- **skills**：领域方法论，编排 tools + LLM prompt（专业知识放 prompt，不放代码）
- **tools**：原子能力，无业务状态；新视觉/提取能力进 tools（如 pdf_vision）
- **storage**：唯一碰 PG/SQLite 的层；DDL 全部集中在 `pg_schema.py`/各模块 ensure 函数

**写代码前先看**：`core/config.py`（配置优先级 env > yaml）、`api/deps.py`（require_admin 是
DB 实时角色校验）、`storage/pg.py`（PgClient 是上下文管理器，事务自动提交/回滚）。

## 3. 数据流水线（研报链路，最常改动）

```
07:00/23:00 调度 → 爬话题 + 逐个下载PDF(反检测,60-120s/个)
  ※ 中文翻译版(文件名"中文版-"等, is_chinese_translated)下载/入库层直接过滤, 保留英文原版
  → 每下载1个立即(逐篇流水, 下载完即"已分析"前端可用):
     sync_zxsq_to_pg 入库(返回新report_id) → LLM Analysis(仅文件名→标题/机构/标的/行业/地区/市场)
     → 深度提取(正文→评级/盈利预测/tags, 单篇, 输出一律中文) → merge_report_duplicates 去重(txt并入PDF)
  ※ 深度提取也可由研报中心「AI 分析」按钮 SSE 流式触发(弹窗展示 Agent 流程+模型输出,
    POST /api/reports/{id}/analyze/stream; 与批量链路共用 _apply_extract_result)
  ※ 轮末批量提取(限额8/15篇)与 analyze_pending 仅作兜底清积压, 不在主链路上
图片型PDF(无文本层) → 后台线程 qwen 视觉OCR(状态 pending_ocr, 前端隐藏)
  → OCR完成后自动接续单篇深度提取(不等下一轮调度)
```
关键表：`fin.report_meta`（file_name 原名/title 清洗后/org/target/industry/region/market）、
`fin.report_forecast`（盈利预测）。market 词表：A股/H股/TW股/日股/韩股/美股/欧洲股/东南亚股/商品/宏观/其他。

## 4. 模型路由（改模型只动 config.yaml）

`config.yaml llm.models.<用途>`：default/agent/extract/analysis=deepseek-v4-flash-0731，
vision=qwen3.8-flash（deepseek 不收图片）。**思考开关由 `llm.enable_thinking` 全局控制
（当前 true=最强思考；vision 用途已覆盖关闭）；`llm.max_tokens` 必须给足（16384，思考与
正文共用预算）——两者由 `llm/client.py` 统一注入，业务代码禁止传 `enable_thinking`**。
代码用 `get_agent_llm()/get_extract_llm()/get_vision_llm()` 工厂，禁止硬编码模型名。详见
[docs/agents/llm_models.md](docs/agents/llm_models.md)。

## 5. 认证与角色（改权限先读）

- 全 API 需 JWT（登录/注册/health 白名单）；注册需邀请码（.env `AUTH_INVITE_CODE`）
- 三级角色：超级管理员（.env `SUPER_ADMIN`，用户管理独占）/ admin（区间看板）/ user
- `require_admin` 查 DB 实时角色（不信 token 内 role）——提权/降权即时生效
- `/api/range/*` 整组 require_admin；`/api/auth/users*` 整组仅超级管理员
- 种子管理员仅当用户表为空时创建（防删除后重启复活）

## 6. 已踩过的坑（新代码必读，防复发）

| 坑 | 规则 |
|----|------|
| deepseek 思考吃光 token（content 空） | 根因是 **max_tokens 太小**而非思考本身：开思考必须配足 `llm.max_tokens`（16384）；思考开关 config 统一控制，业务代码禁止硬编码 |
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
| LLM prompt 花括号 | `.format()` 模板里的 JSON 花括号必须转义 `{{}}`（曾致 KeyError） |
| 视觉 OCR 阻塞请求线程 | OCR 一律后台线程 + 状态 pending_ocr（前端隐藏，完成后自动接续） |
| PG 服务端重启杀数小时批任务 | 长批任务按工作单元（每股）捕获 OperationalError 重连续跑（batch.py `_process_stock`/sync_holders 模式）；注意 PgClient 连接在 `__enter__` 才从池获取，手动管理必须 `PgClient().__enter__()` 成对使用，裸构造 conn/cur 全是 None |

## 7. 开发规范（提交前自查）

1. **加表/加列**：DDL 写进 `pg_schema.py` 或模块内 ensure 函数；用 `ADD COLUMN IF NOT EXISTS`；
   独立连接执行，绝不放在业务事务里
2. **加 API**：想清楚挂在哪个 router；除登录/注册外自动被鉴权中间件覆盖；管理类加
   `require_admin`；重逻辑 `asyncio.to_thread`
3. **加 LLM 调用**：用 `llm/client.py` 工厂（purpose 路由），思考/max_tokens 由 client
   统一注入（业务层不传）；JSON 输出的 prompt 用 `{{}}` 转义；解析用宽容提取（剥 ```json 块）；
   面向用户的输出在 prompt 里显式要求中文
4. **加前端页**：`web/src/` 下新组件 + `Platform.jsx` NAV 注册（注意 adminOnly/superOnly）；
   改完必须 `npm run build`（后端托管 dist，不构建不生效）
5. **验证**：改研报链路后跑 `python -m tools.finance.report_meta_analysis` 单篇验证；
   改量化跑 `pytest range_trading/tests`；改 beta_alpha 跑 `pytest agent/beta_alpha/tests`
   （改链模板另跑 `python -m agent.beta_alpha.analysis.chain_analysis <chain_id>`）；
   改完 git commit（消息带日期与模块）
6. **日志**：一律 `get_logger(__name__)`（core/logger 按顶层包聚合落
   logs/agent.log / tools.log 等；禁止裸 `logging.getLogger` 与库代码 print；
   CLI 最终汇总/报表输出可保留 print）；级别/保留天数在 config.yaml `logging` 段

## 8. 文档地图（深入阅读）

| 模块 | 文档 | 一句话 |
|------|------|--------|
| 前端登录 | [docs/frontend/login.md](docs/frontend/login.md) | JWT/邀请码/三级角色/校验规则 |
| 前端问答 | [docs/frontend/chat.md](docs/frontend/chat.md) | SSE 协议/意图路由 |
| 前端研报 | [docs/frontend/reports.md](docs/frontend/reports.md) | 流水线/表结构/去重/LLM Analysis/AI分析流式弹窗 |
| 前端框架 | [docs/frontend/framework_theme.md](docs/frontend/framework_theme.md) | 品牌/深黑暗棕主题/桌面入口 |
| 模型体系 | [docs/agents/llm_models.md](docs/agents/llm_models.md) | 用途路由/两个模型的坑 |
| 扫描件 Agent | [docs/agents/scanned_report_agent.md](docs/agents/scanned_report_agent.md) | PDF→PNG→视觉OCR 解耦设计 |
| MCP 工具层 | [mcp/README.md](mcp/README.md) | 18 工具/ToolSpec 规范/中文description约定/stdio 服务(默认只读) |
| Agent 对话循环 | [docs/agents/agent_loop.md](docs/agents/agent_loop.md) | LLM 自主决策+工具循环/多轮追问/累计口径知识/沙盒；新入口 `/api/v1/agent/stream` |
| 内容资产管线 | [docs/agents/content_pipeline.md](docs/agents/content_pipeline.md) | 上市公司画像/公众号文章/pptgen出片；`python -m agent.content` |
| Beta/Alpha Skill | [docs/agents/chain_beta_skill.md](docs/agents/chain_beta_skill.md) | 产业链种子链/三源映射/环节指数/预期差四象限；Agent 会话入口在 `.agents/skills/beta-skill`、`.agents/skills/alpha-skill` |
| 安全 | [docs/ops/security.md](docs/ops/security.md) | 分层防护/事件审计/加固清单 |
| 数据处理 | [docs/ops/data_process.md](docs/ops/data_process.md) | 历史：同步全流程 |
| 爬虫 | [docs/ops/spider.md](docs/ops/spider.md) | 历史：反检测设计 |
| 量化 | [docs/quant/](docs/quant/) | 震荡/趋势/动量矛三指南 |
| 产品全貌 | [docs/PRD.md](docs/PRD.md) | v1.0 需求与架构（部分过时，以本文件为准） |

## 9. 文档维护约定（写文档前读）

1. 一模块一文档，先归档进 docs/ 对应子目录，并更新 README 索引
2. 统一五段结构：**系统设计 → 需求优化 → 问题 → 处理方案 → 验证记录**
3. Bug 修复必须留痕（根因+方案写入该模块「历史问题与处理记录」表）
4. 与代码实态冲突时以代码为准，并顺手修正文档
5. 地区名词用代号 TW/HK；bat/配置文件避免中文（编码坑）
6. 公开仓库不写部署细节与访问地址（内部运维文档除外且不含真实地址）
