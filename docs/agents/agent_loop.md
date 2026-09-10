# Agent 对话入口（MCP 工具循环）设计文档

> Alpha Finance Radar · 对话 Agent 新范式
> 更新：2026-09-07

## 一、系统设计

### 1.1 范式转变

```
旧 (意图路由+固定管道):                新 (LLM 自主决策+工具循环):
  意图分类器 (7 格子)                    LLM 看全部 17 个只读工具
  → 命中即锁死单条管道                   → 自己决定调什么、调几次
  → 复合问题答不了                       → 组合查询/交叉验证/自由问答
  → 每加能力改 3 处路由                  → 加能力=注册一个工具
```

### 1.2 链路

```
AgentChat.jsx (session_id 每页生成一次, 全程携带)
  → POST /api/v1/agent/stream (SSE)
    → agent/loop.run_agent_stream:
        ├─ 快车道: L1 三重门槛命中 → 直连 Text-to-SQL (零 LLM, 0.4~2s)
        ├─ 域子Agent: L1 规则单域命中 (agent/subagent.py, 6 域) →
        │   域内 2~4 工具 + escalate 伪工具 + 域专家提示词 → 共享决策循环
        │   └─ escalate_to_full_agent → 同一 messages 换全工具清单续跑 (上下文不丢)
        └─ 全工具循环: 未命中/复合/定义类/升级续跑 (兜底永在)
            loop (≤8 轮): deepseek function-calling × 17 只读工具
            ├─ 同轮多调用并行执行 (gather)
            └─ 执行层白名单: 只跑本次 manifest 列出的工具
    → agent/history_store (内存会话历史, 30min TTL)
    → mcp.bridge.openai_tools() (只读 17 工具) / mcp REGISTRY.call
```

**域子Agent 设计要点**（`agent/subagent.py`）：
- 路由 = L1 纯规则（毫秒级零 LLM）：6 域核心词恰命中 1 个 → 该域；
  ≥2 个（复合）或 0 个 → 全工具循环。**路由是缩小菜单的优化，不是能力闸门**
- 定义/解释类问法（什么是…）一律全循环
- escalate 伪工具只注入域清单（不进全局注册表/MCP）；
  子 Agent 发现需要域外能力时主动申请升级
- 执行层白名单：模型幻觉调用清单外工具名会被拦截并引导走 escalate

**快车道三重门槛**（宁漏勿误，误走会把复合/常识问题截胡答一半）：
① L1 命中 query/compare 且置信 ≥0.85；② 文本含股票实体；③ 不含深水区词
（分析/怎么看/分位/预期差/研报/一起…见 `_DEEP_WORDS`）。

**快车道 NL2SQL（v3, 2026-09-08）**：LLM 生成 SQL（主）+ 规则引擎（兜底，30s 超时回落）。
- 动机：规则引擎对「最近三年」这类相对时间**不生成过滤**（recent_years 分支落空，
  实测 WHERE 无 end_date 条件、LIMIT 48 取了 12 年），时序口径失真
- 实体解析仍在规则层完成（毫秒级、零幻觉），LLM 只负责拼 SQL
- 时延关键：`llm.models.nl2sql` 用途**关思考 + max_tokens 2048**（config 层配置，
  遵守「思考开关 config 统一控制」约定）——思考版实测 4~27s，关思考 2~6s
- 口径修正：「最近N年」= 最近 N 个**年报**（子查询取 12-31 期 LIMIT N），
  明说"季度/报告期"才取全部报告期；实测 最近三年=3 行、8 报告期=8 行、双股对比=2 行
- 生成 SQL 必过 sql_guard（只读/schema 白名单/LIMIT）；校验失败同样回落规则引擎

### 1.3 SSE 事件协议（兼容旧链路，前端零改动可用）

| event | data | 说明 |
|-------|------|------|
| `stage` | `{stage:"tool", message:"调用 query_financials(stocks=中际旭创)…"}` | 工具执行进度（步骤卡片） |
| `tool_call` | `{tool, args, ok, elapsed_ms, rows}` | 工具结果元信息（新事件；步骤补耗时/行数/失败标记） |
| `data` | `{intent:"agent", tool, data, columns, rows}` | 表格型工具结果 → 图表渲染（多个时前端显示最后一个） |
| `delta` | `{text}` | 最终回答 80 字符分块（打字机效果） |
| `done` / `error` | `{}` / `{message}` | 同旧链路 |

### 1.4 关键文件

| 文件 | 职责 |
|------|------|
| `core/agent/loop.py` | Agent 主循环：系统提示词（口径知识/数据纪律/多轮规则）、沙盒参数、事件流 |
| `agent/history_store.py` | 会话历史（内存，完整轮配对注入，6 轮/6000 字符双限截断） |
| `core/llm/client.py` | 新增 `invoke_with_tools`（function-calling，返回完整 message） |
| `api/finance.py` | 新端点 `POST /api/v1/agent/stream`（旧 `/query/stream` 保留作回退） |
| `web/src/AgentChat.jsx` | 切换到 agentStream + session_id + 🤖 徽章 + 工具步骤元信息 |
| `mcp/server.py` | stdio MCP 服务默认只读模式（`ALPHA_MCP_READ_ONLY=1`） |

## 二、需求与设计决策

| 需求 | 决策 |
|------|------|
| 随意问（BI+延伸+相关问题） | LLM 自主路由；平台数据必须走工具（反幻觉纪律写进 system prompt）；通用常识直接答并声明"非平台数据" |
| 追问质疑（"环比竟然下降了，数据对吗"） | ① 会话历史注入（工具摘要含股票/指标）；② system prompt 内置**累计口径知识**：相邻报告期相减才是单季，Q2单季=中报−Q1…；③ 质疑类必须**重新取数核验**，禁止凭上轮记忆辩解 |
| 只读安全（一半以上是查询） | 只暴露 read_only=True 的 17 个工具；SQL 工具内已有 sql_guard；MCP 服务默认只读模式 |
| 沙盒 | 每工具超时（默认 120s，重扫描 900s）；决策轮数 ≤8；同参数重复调用 ≤3 次；工具结果注入 LLM 截断 4000 字符；底座行数上限 |
| MCP server 复用 | 同一份 ToolSpec 双导出：OpenAI function-calling（内部循环）+ MCP stdio（外部宿主），能力永不分叉 |
| **工具选择优化（2026-09-08）** | ① 股票类工具自解析（直接收股票名），`resolve_stock` 降级为身份查询/多实体/歧义场景，系统提示词明令"不要当前置步骤"——省一次 LLM 决策轮；② 重叠工具家族补交叉引用（get_forecasts/verify_forecasts/analyze_alpha、search_reports/read_report/extract_document、run_quant_scan/compute_indicators）。**注意工具数量对选择的影响与 MCP server 拆分无关**（宿主拍平清单）；若真实误路由持续，下一步为域分组 sub-agent（REGISTRY 按 domain 过滤 manifest 即可，无需拆 server） |

## 三、问题（已踩坑）

| 问题 | 根因 | 处理 |
|------|------|------|
| **规则引擎 NL2SQL 丢失时间口径：「最近三年」生成 48 期（12 年）** | `sql_builder` 的 recent_years 时间分支未落到 WHERE（实测无 end_date 条件），纯关键词拼 SQL 对相对时间/复合条件天然不稳 | 快车道改 **LLM 生成 SQL**（nl2sql 用途关思考 2~6s）+ 规则引擎兜底；prompt 内置口径规则（最近N年=最近N个年报）；生成 SQL 仍过 sql_guard |
| **子Agent 幻觉调用清单外工具**（quant 域直接调了 query_financials 等 5 个域外工具且全部执行成功） | 域清单只约束「模型看得到的菜单」，执行层不校验工具名——deepseek 猜中了全局注册表里存在的名字就越权执行了 | 执行层**白名单**：只跑本次 manifest 列出的工具；越权调用返回教学式错误（引导走 escalate_to_full_agent） |
| **chain 域子Agent 按环节逐个刷 analyze_chain（9 连调, 60s）** | 域提示词没说整链一次调用即可，模型把 12 个环节拆成 12 次单环节查询 | ① 域提示词明确「整链问题只调一次（不带 node 即返回全部环节）」；② 同名工具次数护栏 MAX_SAME_TOOL_CALLS=4；实测降到 1 次调用 |
| **快车道 v1（带 LLM 解读）比 Agent 循环还慢**（28~40s vs 12s） | 瓶颈不在路由而在解读生成：RESULT_INTERPRET_PROMPT v2 要求深度分析，deepseek 思考+长输出 30s+，省掉的 1-2 轮决策抵不过 | 快车道改**零 LLM**：SQL+图表+规则简报（`_result_brief`），0.4~2s；深度解读由追问触发 Agent 循环完成 |
| **「什么是市盈率」被快车道劫持去硬跑 SQL**（评测首跑即抓到） | 「市盈率」是 L1 核心词（0.9 置信），纯关键词无法区分常识问答与数据查询 | 门槛②：快车道必须含**股票实体**（无实体的常识/库外标的问题交 Agent 循环） |
| **「最新市盈率和估值分位一起看看」被快车道截胡只答一半** | 复合意图含快车道词（市盈率），直连管道只查了行情宽表，估值分位丢失 | 门槛③：**深水区词防护**（分析/怎么看/分位/一起/研报…命中即走 Agent 循环）；并新增复合意图评测用例防回归 |
| 模型在回答末尾输出「[本轮工具调用] ✓ search_reports(…) …」清单给用户 | 工具摘要原来拼在**注入历史的 assistant 正文**里，deepseek 把它当成回答格式的一部分模仿输出了（还会自行加戏写"只命中1篇无关研报"） | ① 摘要移出正文，改为末尾一条 **system 角色后台记录**（明示"严禁输出/复述/模仿"）；② system prompt 表达规则加"绝不向用户输出工具调用记录/日志"双保险 |
| 追问轮回答"没有上下文"（测试期） | 测试脚本两次 `python -m` 是两个进程，内存态历史丢失；线上长驻 FastAPI 无此问题 | 测试改单进程双轮；如未来多进程部署需把 history_store 落 DB/Redis |
| 历史注入出现孤儿 assistant 消息（user,user,assistant,user） | `_turn_to_messages` 对 assistant 轮又附了一条 user 消息；倒序逐轮取会拆散 user/assistant 对 | 重写为**完整轮配对**（user+其 assistant），取最近 N 轮，字符双限截断 |
| 底层扫描器 print 排名会污染 SSE/stdio 流 | range_trading 用 print 输出 | api 端点与 mcp server 均以 `redirect_stdout(stderr)` 包裹工具执行 |
| `wait_for(to_thread)` 超时后线程仍在跑 | Python 线程不可杀 | 已知限制：超时即向 LLM 返回错误并继续对话，后台线程自行跑完不阻塞 |

## 四、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-08 | **子Agent 分域路由上线**：E2E ①chain 域 12 环节问题 1 次调用完成（修复前 9 连调）；②quant 域被问"形态×基本面"复合诉求 → 主动 escalate → 全工具接管完整研判（升级链路含 tool_call 事件可观测）；③全量评测回归 24/24 (100%)，路由零破坏 | ✅ |
| 2026-09-08 | **工具选择优化后评测**：21 用例 24 轮全过（新增 identity 身份问句/单维度直达 2 用例）；工具调用对比：resolve_stock 9→1 次（仅剩为身份问句的正确用法），总调用 41→36（用例还多 2 个），覆盖 distinct 工具 12→14，报告 `evals/results/agent_eval_20260908_120747.json` | ✅ 100% |
| 2026-09-08 | **Agent 评测基线（正式）**：19 用例 22 轮全过（query/compare/mainbiz/valuation/forecast/verify/risk/chain/alpha/quant/knowledge/no_data/offscope/compound/multiturn 6/6），报告 `evals/results/agent_eval_20260908_111352.json`。评测过程中抓到并修复：快车道劫持常识问题、复合问题截胡两坑（见问题表） | ✅ 100% |
| 2026-09-08 | 快车道（零 LLM 版）延迟实测：简单查询 0.4~1.9s（Agent 循环 ~12s，带 LLM 解读版 28~40s）；同轮并行执行单测验证（3 调用 0.3s 并行 < 0.8s 断言）；复合问题全栈验证 resolve_stock→(query_financials ∥ valuation_percentile) 双数据完整作答 | ✅ |
| 2026-09-07 | 工具记录泄漏修复后复测：T1「中际旭创的券商盈利预测怎么看」（5 连工具，含 1 次失败）回答无 `[本轮工具调用]` 块；T2「那它的估值现在贵不贵」继承标的、仅 1 次估值工具、无泄漏 | ✅ |
| 2026-09-07 | **E2E 用户原例**（单进程双轮，真实 LLM+PG）：T1「中际旭创2025年报净利润」→ "107.97 亿（累计口径）"；T2「环比竟然下降了，数据对吗？」（零线索）→ 继承标的+指标 → 重新取数拆单季 → "单季逐季抬升，没有环比下降，是累计口径假象；同比 +109%" | ✅ |
| 2026-09-07 | pytest `agent/tests` + `mcp/tests`（提示词知识/历史配对截断/TTL/沙盒参数/只读清单/表格提取） | 31 passed |
| 2026-09-07 | E2E T2 耗时 14s / 工具 1 次（history 生效省掉 resolve_stock） | ✅ |
| 2026-09-07 | `npm run build` 前端构建 | ✅ |

## 五、后续规划

- 最终回答改真 token 流（当前决策轮非流式+分块下发；需在 stream 模式累积 tool_call delta）
- history_store 落 PG/Redis（多进程/重启持久化）
- 对话内文档上传（extract_document 已就绪，差前端上传通道）
- 旧 `/query/stream` 意图管道观察一段后下线
- 评测集扩容（当前 19 用例 22 轮基线），把失败用例修复纳入常规迭代

### 5.1 Agent 评测（evals/run_agent_eval.py）

- 用例集：`evals/agent_cases.py` — 19 用例 22 轮，覆盖 query/compare/mainbiz/valuation/
  forecast/verify/risk/chain/alpha/quant/knowledge(纯常识)/no_data(未来期间)/
  offscope(库外标的)/compound(复合意图)/multiturn(多轮追问)
- 断言：`tools_any`（工具路由正确）/`tools_none`/`expect_no_tools`（常识不调工具）/
  `contains_any|all`（回答关键词）/全局禁词（`[本轮工具调用]` 泄漏防回归）
- 运行：`.venv\Scripts\python.exe evals\run_agent_eval.py`（默认快车道开=线上路径；
  `--no-fast-lane` 强制全走 Agent 循环；`--only multiturn` 按类型过滤）
- 报告：`evals/results/agent_eval_<ts>.json`（含 by_type 分组通过率）
