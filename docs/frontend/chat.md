# 问答（Agent Chat）设计文档

> Alpha Finance Radar 平台 · 智能问答子系统
> 更新：2026-11-12（对话工作台、会话隔离与流式协议修复）

## 一、系统设计

### 1.1 交互链路（SSE 流式）

```
用户输入 ──> AgentChat.jsx (send)
              │  fetch POST /api/v1/agent/stream (ReadableStream 逐块解析, session_id 全程携带)
              ▼
          api/agent_api.py agent_stream_endpoint → harness/loop.py run_agent_stream
              │
              ├─ ① 快车道: 高置信简单 query/compare → LLM 直生成 SQL(规则兜底) → 图表+简报
              ├─ ② 域子Agent: L1 规则路由五域(财务/研报/产业链/风控/实体), 域内 2~4 工具
              │     + escalate_to_full_agent 升级伪工具
              └─ ③ 全工具循环: 29 只读工具 LLM 自主决策 (快车道/子Agent 未命中或升级续跑)
```

（旧链路 `POST /api/v1/query/stream` 与编排层 services/ 已于 2026-09-14 移除，
对话统一走 harness 单引擎）

### 1.2 SSE 事件协议

| event | data | 说明 |
|-------|------|------|
| `stage` | `{stage, message}` | 阶段进展（意图识别/工具调用/升级接管…），UI 显示进度气泡 |
| `tool_call` | `{tool, args, ok, elapsed_ms, rows}` | 工具结果元信息（Agent 循环新增） |
| `data`  | `{intent, tool, data, columns, sql, rows}` | 结构化查询结果（表格一次性下发） |
| `delta` | `{text}` | 最终回答文本块（多次，打字机效果） |
| `done`  | `{}` | 结束 |
| `error` | `{message}` | 失败 |

### 1.3 关键组件

| 文件 | 职责 |
|------|------|
| `web/src/chat/AgentChat.jsx` | 对话 UI：消息流、类型徽章、阶段气泡、结果呈现路由（图表/表格）、示例问题 |
| `web/src/chat/ResultChart.jsx` | 结果可视化：`buildChartSpec` 形态识别 + 折线图(lightweight-charts)/柱状图(SVG)；共享展示工具（COL_LABELS/parseNum/compactNum/fmtDateCol） |
| `web/src/platformApi.js agentStream()` | SSE 客户端：fetch + ReadableStream 手动解析 `event:`/`data:` 行 |
| `api/agent_api.py /agent/stream` | SSE 服务端（决策轮非流式；最终回答按块下发） |
| `harness/loop.py` | Agent 主循环：快车道 → 域子Agent → 全工具循环 + 沙盒（详见 docs/agents/agent_loop.md） |
| `harness/intent.py` | 分层意图识别（核心词规则 + LLM 兜底；快车道/子Agent 路由复用其 L1） |

### 1.4 轻量 Markdown 渲染

回复气泡内用自研 `Markdownish` 组件渲染：`#`/`##`/`###` 标题、`**加粗**`、`-` 列表、段落——不引外部库，覆盖 LLM 输出的主流格式。流式输出期间气泡末尾带闪烁光标（`.chat-bubble.streaming`）。

### 1.5 结果可视化与表格呈现（2026-09-02 四次迭代：同花顺式柱状图）

查询结果**图表优先、表格兜底**，由 `resultView()`（AgentChat.jsx）+ `buildChartSpec()`（ResultChart.jsx）两级判定：

**图表形态识别（buildChartSpec）**

| 形态 | 条件 | 渲染 |
|------|------|------|
| 分期柱状图 | 报告期列(end_date/ann_date) ≥2 个不同值 + 1~4 个数值列 + 单实体 | 同花顺式 SVG 柱状图：**每个报告期独立一根柱，不连线**（Q1/H1/Q3/年报是累计口径，互相不可比，折线连线有误导性）；X 轴标签 短年份+期别（24Q1/24H1/24年报），全年报时为 14年…25年；Y 轴 0 基线+紧凑刻度(亿/万)；正值用系列色、负值绿色；柱子带原生 tooltip（期别+指标+精确值）；多指标分组柱 |
| 年度聚合 | 报告期 >20 期（季度数据跨 >5 年）时自动只取年报行（≥5 根才聚合） | 12 年数据 → 12 根年度柱，避免 40+ 根拥挤柱 |
| 折线图 | 日线序列(trade_date) + 1~4 数值列（连续交易日连线有意义） | lightweight-charts v5，十字线取值，Y 轴紧凑数值 |
| 横向条形图 | 多实体（对比/行业排名）：名称列 + 恰好 1 个数值列，2~30 行 | 自绘 SVG，A股红涨绿跌（正值 #f43f5e / 负值 #10b981），零轴支持负值，>12 行截断注明 |
| 表格兜底 | 不可图示（如行情宽表 >4 个指标列、非数值列） | 原精简表格（限高 320px + 吸顶表头） |

**数据范围**：`sql_builder.build_single_query` LIMIT 48（约 12 年季度），前端按需聚合为年度柱——图表默认呈现 10 年+ 历史（此前 LIMIT 12 仅 3 年）。

**通用展示规则**

- `ts_code` 一律隐藏；单值列（股票名/报告期）折叠为图表标题（如「贵州茅台」）
- 表头 `COL_LABELS` 映射中文（40+ 项）；报告期 `2023-12-31` → `2023 年报`
- 数值千分位；PG NUMERIC 到前端是**纯数字字符串**（Decimal→json default=str），需按字符串数值解析（`parseNum`）
- 图表图例显示指标名 + 最新值（紧凑格式 `compactNum`：1688.4亿）+ 最新报告期

**关键实现坑**：流式解读期间父组件每次 delta 都重渲消息，`buildChartSpec` 每次返回新对象——lightweight-charts 实例以 `JSON.stringify(spec)` 内容为 effect 依赖，避免被反复销毁重建。

### 1.6 结果解读 Prompt 策略（v2, 2026-09-02）

`skills/fin_query/prompts.py RESULT_INTERPRET_PROMPT` v2——数值已由前端图表呈现，解读不复述数据：

| 原则 | 说明 |
|------|------|
| 禁止数据概览 | 不罗列"最新值为X/各期分别为X"流水账，用户看图即可知数值 |
| 趋势与拐点 | 加速/放缓/突变/背离，给量级感觉（"增速腰斩"）而非精确复述 |
| 深层原因挖掘 | 结合行业、商业模式、量价、周期位置、政策，推测驱动因素 |
| 行业坐标 | 龙头/均值/落后，与可比公司对比 |
| 后续观察点 | 值得跟踪的指标/事件 1-2 条 |
| 格式 | 开头一句话直接回答；3-5 个要点，每点加粗结论+展开；不做投资建议 |

### 1.7 回答类型图标徽章（2026-09-02 二次迭代）

后端 SSE 一直在 `data` 事件携带 `intent`，前端此前未消费——现已接入，每条 AI 回答顶部显示类型徽章（`INTENT_META`）：

| intent | 徽章 | 配色 |
|--------|------|------|
| query | 📊 财务数据 | 品牌棕红 |
| compare | ⚖️ 对比分析 | 金色 |
| report | 📄 研报解读 | 紫色（与研报标签色一致） |

- 意图在**首个 stage 事件**（"意图识别: X"）即解析出，徽章随进度提示即时显示，无需等结果。
- 空状态示例问题带类型图标（📊/📄/🪙），暗示问答能力分布。

### 1.8 滚动体系（2026-09-02 优化）

- 全局自定义滚动条（`::-webkit-scrollbar` + `scrollbar-width: thin`）：深棕细圆角，hover 提亮，全站（研报/区间看板）统一。
- 消息流 `.chat-window` 是唯一纵向滚动容器（`overscroll-behavior: contain`，滚动不穿透主框架）。
- **粘底跟随**：`stickRef` 记录用户是否在底部（距底 <80px）；流式输出/新消息仅在贴底时自动滚到底，**用户上翻查看历史时不被打扰**（旧实现 `scrollIntoView` 每个 delta 强制拽底，已废弃）。
- 上翻距底 >80px 时右下角浮现「回到底部」按钮（`.chat-jump`），点击平滑滚回。

## 二、需求优化

### 2.1 对话工作台（2026-11-12）

- 新增会话列表、新建/切换/删除会话、独立草稿；按登录账号隔离 sessionStorage，最多 20 个会话、每会话 100 条消息。
- 多行自适应输入，Enter 发送、Shift+Enter 换行；组合输入期间 Enter 不发送。生成时可写下一条草稿。
- 停止按钮与 Esc 中断请求消费；错误单独展示并可重试，不覆盖已有回答；断流保留部分文本。
- 研究过程默认折叠，正文保持图表/表格；支持复制回答、围栏代码、行内代码及安全 HTTP(S) 链接。
- 细屏折叠历史栏和平台导航，支持键盘焦点、触控和减少动画偏好。
- `platformApi.agentStreamRaw` 返回 `{promise, abort}`，旧 `agentStream` Promise 接口保留。
- `sse.js` 处理 LF/CRLF/CR、多行 data、跨字节 UTF-8 与无终止空行尾帧。
- Agent 请求以 Pydantic 校验非空文本、16000 字符上限、session_id 长度与格式；服务端通过 user_id 对 session_id 加命名空间。
- 标签页关闭后本地记录结束；服务端仍为 30 分钟内存会话，重启/过期后不能根据可见历史推定模型仍有上下文。
- 停止不等于强杀同步后台调用；当前 harness 最终回复仍是全文生成后分块输出。

### 2.2 意图路由规则（现行）

| 意图 | 触发词示例 | 链路 |
|------|-----------|------|
| query | 营收/净利润/毛利率/查询… | Text-to-SQL |
| compare | 对比/谁更高/两个以上股票实体 | Text-to-SQL（对比 SQL） |
| report | 研报/解读/观点/评级/目标价/盈利预测/黄金/原油/贵金属/大宗商品 | ReportSkill |
| detect / verify | 财务风险/研报校验 | 占位（M3/M4 后续） |

## 三、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 折线图把 Q1/H1/Q3/年报放到同一坐标轴上连线，口径误导 | 财报各期是**累计口径**（Q1=Q1累计、H1=上半年累计），连线暗示连续可比 | 报告期序列改为同花顺式**分期柱状图**（每期独立柱不连线）；日线(trade_date)保留折线 | ✅ 2026-09-02 |
| 图表只有约 3 年数据 | `build_single_query` LIMIT 12（12 个季度） | LIMIT 48（12 年）+ 前端长序列(>20 期)自动聚合为年度柱，默认呈现 12 年年报 | ✅ 2026-09-02 |
| 解读文本在罗列数据概览，与图表重复、缺乏增量 | 旧 prompt 要求"总结关键数据" | RESULT_INTERPRET_PROMPT v2：禁止数据概览，聚焦趋势拐点/行业坐标/深层原因/观察点（见 1.6） | ✅ 2026-09-02 |
| 时间序列结果仍以「报告期/营业收入」表格呈现，信息密度低 | 前两轮仅做列裁剪，未改变表格形态 | 结果图表化：时间序列→折线图（营收 12 期趋势一目了然），对比/排名→柱状图；表格仅在不可图示时兜底 | ✅ 2026-09-02 |
| 需要数据可视化能力（折线图/柱状图） | 无结果图表组件 | 新增 `ResultChart.jsx`：折线复用 lightweight-charts v5（与区间看板同栈），柱状为自绘 SVG（红涨绿跌、零轴负值支持、>12 行截断） | ✅ 2026-09-02 |
| 图表实例在流式解读期间被反复重建（潜在闪烁/性能） | 每个 delta 触发父组件重渲，spec 对象每次新建，effect 依赖失效 | LineChart 以 `JSON.stringify(spec)` 内容为依赖键 | ✅ 2026-09-02 |
| 对比结果出现「股票/报告期/数值」表格，与解读文本完全重复 | 规则引擎 `build_compare_query` 固定输出 `s.name, i.end_date, {field} AS value` 三列，前端原样渲染 | 现以柱状图呈现（茅台 vs 五粮液直接可视对比），图表不可用时回退表格 | ✅ 2026-09-02 |
| 表头出现「报告期」整列重复同一日期 | 单股/行业查询各行为同一报告期时 `end_date` 逐行重复 | 单值 报告期/公告日/股票名 列折叠进表格标题（「贵州茅台 · 2026 中报」）；多值时保留（时间序列行标识） | ✅ 2026-09-02 |
| 回答无类型指示，用户分不清走的是数据查询还是研报解读 | SSE `data` 事件的 `intent` 字段前端一直未消费 | 接入意图图标徽章（📊/⚖️/📄），首个 stage「意图识别」即解析提前显示 | ✅ 2026-09-02 |
| 表格出现 `ts_code`/`stock_name` 两栏冗余身份列 | `sql_builder.build_single_query` 固定 SELECT `i.ts_code, s.name`，前端原样渲染 columns | 双端处理：SQL 模板去掉 ts_code（skill.py `_rule_based_sql` 同步）；前端 `tableMeta()` 隐藏 ts_code、单值名称列折叠为标签 | ✅ 2026-09-02 |
| 金额显示 `90703260964.4800` 原始串 | PG NUMERIC → pandas Decimal → `json.dumps(default=str)` 到前端是字符串，绕过 `typeof==='number'` 格式化 | `fmtCell` 用正则识别纯数字字符串转 Number 再千分位格式化 | ✅ 2026-09-02 |
| 流式输出时页面被强制拽到底部，无法上翻看历史 | 旧实现每个 delta 触发 `endRef.scrollIntoView` | 改为 `stickRef` 粘底跟随 + 「回到底部」悬浮按钮 | ✅ 2026-09-02 |
| 长表格把对话页撑得极长 | 表格无高度限制 | `.chat-table` 限高 320px 内滚 + sticky 表头 | ✅ 2026-09-02 |
| 「看看黄金的观点」被路由到 query（返回 A 股黄金板块行情） | `黄金` 命中 A 股股票词典优先于 report 意图 | `agent/intent.py` CORE_KEYWORDS.report 增加 观点/黄金/原油/贵金属/大宗商品 | ✅ 已修复 |
| 长回答需等 15-20s 才一次性出现 | 无流式实现 | 本轮整体实现 SSE（见 1.1），stage 提示进度、delta 打字机输出 | ✅ 本轮 |
| SSE 经公网域名（Cloudflare Tunnel）是否正常 | — | 实测 71 个事件完整下发（3 stage + 1 data + 66 delta + 1 done），cloudflared 原生支持 SSE | ✅ 已验证 |
| 前端逐字节读流导致中文乱码（验证期） | 测试脚本按单字节读把 UTF-8 多字节字符拆坏 | 属测试脚本问题；正式实现用 TextDecoder stream 模式，无此问题 | ✅ 非缺陷 |

## 四、处理方案与后续规划

- 研报链路的 delta 也改为逐 token 流（当前 ReportSkill 内部聚合多篇研报后一次性产出）
- ~~WebSocket 通道（`api/ws.py` 已有骨架）与 SSE 并存或合并~~（2026-09-14：ws.py 骨架随
  core/workflow 旧引擎一并移除，流式统一走 SSE）
- 历史问题表所记均为旧链路（/query/stream 时代）问题，Agent 循环沿用同一 SSE 协议与
  前端渲染，图表/滚动相关结论继续有效


2026-11-17模块收敛：`chat/resultData.js`提供纯数据格式与图形判别，`ResultChart.jsx`只渲染，
`chat/ResultLineChart.jsx`在真正需要日线走势图时加载绘图库；`lib/http.js`统一SSE与JSON/下载鉴权。
保留表格、累计季度独立柱与行情连续线的原选择规则，新增纯数据及中文跨字节SSE测试。

## 五、验证记录

2026-11-12：SSE 协议 3 项 Node 测试通过；前端生产构建通过，lint 无错误（现有混合导出 Fast Refresh 告警保留）。
新增 API 会话隔离、非法输入和历史预算回归测试通过；本地浏览器用模拟流确认回复展示、新建/切换会话、停止生成、草稿及刷新恢复和折叠过程。
完整审核与测试范围见 [技术审核](../ops/technical_review.md)。
