# mcp/ — MCP 工具层 (19 只读 + 3 写类 = 22 个投研能力)

> 2026-09-07 首版。目标: 把平台散落在 skills/tools/beta_alpha/range_trading 的原子能力
> 统一包成 **带 JSON Schema 的标准工具**, 同时服务两条消费链路:
> ① 内部对话 Agent 的 function-calling 循环 (deepseek-v4-flash);
> ② 外部 MCP 客户端 (ZCode / Claude 等) 经 stdio 调用。
> **本目录只做工具化封装, 不改动任何现有业务代码。**

---

## 一、目录结构

```
mcp/
  spec.py       ToolSpec 规范 (一个工具长什么样, 见下节)
  registry.py   注册表: @REGISTRY.tool 装饰器 / call 统一入口 / 双协议导出
  tools/        23 个工具实现 (20 只读 + forge_chain/fetch_annual_report/write_article 3 写类), 按 7 域分组
    _common.py        公共助手 (股票解析/DataFrame 序列化/日K加载)
    entity.py         3 个: 实体与知识
    financial.py      3 个: 财务与行情
    reports.py        4 个: 研报
    chain_alpha.py    4 个: 产业链与预期差
    quant.py          2 个: 技术与量化
    risk.py           2 个: 风控 (全新逻辑, 无既有底座)
  server.py     stdio MCP 服务 (零依赖手写 JSON-RPC)
  bridge.py     OpenAI function-calling 桥 (内部 Agent Loop 用)
../../scripts/smoke_mcp_tools.py   全量冒烟 (fast/heavy 两档)
```

## 二、工具清单 (18 只读 + 3 写类)

| # | 域 | 工具 | 能力 | 底座 (既有代码) | 只读 |
|---|----|------|------|----------------|------|
| 1 | entity | `resolve_stock` | 股票实体解析→ts_code+行业档案 | `tools/finance/stock_kb` | ✓ |
| 2 | entity | `get_schema` | 数据字典问答 | `tools/finance/schema_info` | ✓ |
| 3 | entity | `trading_calendar` | A股交易日历 (last/offset/range/period_ends) | `tools/kline/calendar` | ✓ |
| 4 | financial | `query_financials` | 财务/行情查询 4 模式 (single/compare/industry/rank) | `sql_builder` + `sql_guard` | ✓ |
| 5 | financial | `query_main_business` | 主营构成 (产品/地区/行业占比+毛利率) | `fin.v_main_biz` | ✓ |
| 6 | financial | `valuation_percentile` | PE/PB 近 N 年分位 | `stock.daily_basic` | ✓ |
| 7 | report | `search_reports` | 研报检索 (关键词/标的/标签/评级) | `fin.report_meta` | ✓ |
| 8 | report | `read_report` | 读研报 (元数据+正文节选; 带 question 走 LLM 单篇问答) | `ReportSkill(qa)` | ✓ |
| 9 | report | `get_forecasts` | 盈利预测+分歧度 CV+上修/下修方向 | `report_forecast` + alpha 统计 | ✓ |
| 10 | report | `extract_document` | 本地文档→文本 (扫描件自动视觉 OCR) | `report_extractor` + `tools/pdf` | ✓ |
| 10e | report | `extract_pdf_tables` | PDF 线框表格→结构化行列 (pdfplumber, 不调模型, 数字精确) | `tools/pdf/tables` | ✓ |
| 10b | report | `fetch_annual_report` | 巨潮定期报告拉取 (元数据入库+PDF下载) | `tools/cninfo` 爬虫 | ✗ |
| 10c | report | `build_company_profile` | 上市公司画像 (八大板块数据组装) | `content/profile` | ✓ |
| 10d | report | `write_article` | 公众号文章生成 (画像/研报综述) | `content/article` | ✗ |
| 11 | chain | `list_chains` | 种子链清单 | `beta_alpha/analysis/chain_analysis` | ✓ |
| 12 | chain | `analyze_chain` | 环节→标的映射+环节指数超额收益 | 同上 (`map_chain_members`) | ✓ |
| 13 | chain | `forge_chain` | LLM 生成新链模板 (YAML 草稿, 不落盘) | `beta_alpha/forge` | ✗ |
| 14 | chain | `analyze_alpha` | 预期差四象限 (纯量化) | `beta_alpha/skills/alpha` | ✓ |
| 15 | quant | `compute_indicators` | ADX / POC / Wyckoff Spring | `tools/kline/indicators` | ✓ |
| 16 | quant | `run_quant_scan` | regime 单标的 / range / trend / setup 全市场扫描 | `range_trading` | ✓ |
| 17 | risk | `detect_financial_risk` | 8 规则财务排雷 (毛利率连降/背离/商誉/杠杆…) | **全新** (fina_indicator+三表) | ✓ |
| 18 | risk | `verify_forecasts` | 券商预测兑现校验 (历史预测 vs 实际, 命中率/偏向) | **全新** (report_forecast+income) | ✓ |

## 三、ToolSpec 设计格式 (审核重点)

一个工具 = 一个 `@REGISTRY.tool(...)` 装饰器声明 + 一个普通 Python 函数:

```python
@REGISTRY.tool(
    name="valuation_percentile",            # 英文 snake_case, 动词开头, 全局唯一
    domain="financial",                     # 6 域之一 (分组/权限/图表路由用)
    description="估值分位: 当前 PE(TTM)/PB 处于近 N 年历史的百分位…",  # 中文, 见下节
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年数", "integer", default=3),
    }, ["stock"]),                          # 标准 JSON Schema (required 列表)
    examples=["贵州茅台现在估值贵不贵"],     # 中文示例, 不进协议, 供审核+路由提示
    read_only=True,                          # False = 有副作用 (LLM 生成/写文件)
)
def valuation_percentile(stock: str, years: int = 3) -> dict: ...
```

**返回值统一信封** (registry.call 包装, 两种协议下完全一致):

```json
{"ok": true,  "tool": "...", "elapsed_ms": 356, "warnings": [], "data": {…工具自定义…}}
{"ok": false, "tool": "...", "error": "中文错误原因 (原样回传给 LLM)"}
```

约定:
- handler 一律**同步函数** (重 IO 由上层 `asyncio.to_thread` 包裹), 返回 JSON 可序列化 dict
- 面向用户的可预期失败抛 `ToolError("中文消息")`; 未捕获异常由 registry 兜底, 不炸调用方
- 未在 schema 声明的参数自动剔除 (防 LLM 幻觉参数), 并在 `warnings` 里说明
- SQL 类工具内部强制过 `sql_guard` (只读+schema 白名单+LIMIT)
- Decimal/date/NaN 由 `_jsonable` 统一清洗, 前端不再见到原始字符串数字

## 四、Description 用中文还是英文? —— 结论: **中文 description + 英文 name/参数名**

| 项 | 选择 | 理由 |
|----|------|------|
| 工具 name | 英文 snake_case | function-calling 惯例; 稳定 token, 客户端/日志/权限过滤好处理 |
| 参数 name | 英文 snake_case | 同上; 且 JSON 键避免中文 (部分 MCP 客户端对非 ASCII 键兼容性存疑) |
| description | **简体中文** | ① 主力模型 deepseek 系中文语料最强, 工具路由本质是语义匹配, 中文 query×中文 description 命中率最高; ② 平台输出本来就要求中文; ③ 审核者是中文同事 |
| 参数 description | 简体中文 | 同上 |
| enum 取值 | 英文短词 (single/compare…) | 作为代码标识符稳定; 中文语义写进参数 description |
| examples | 中文示例问题 | 不进协议, 纯审核与提示词素材 |

> 若未来换英文为主的模型 (gpt/claude) 做路由, 中文字段对它们同样可读 (翻译能力足够),
> 而反向 (英文 description 配中文 query) 会明显吃亏——所以中文是单向安全的选择。

## 五、两种消费方式

### 5.1 内部 Agent Loop (推荐主路径, 对话页升级用)

```python
from mcp.bridge import openai_tools, run_tool_call, system_prompt_hint
from llm.client import get_agent_llm

tools = openai_tools()          # 只读 18 个; openai_tools(include_write=True) 全量 21
# … LLMClient 发起 tool_choice="auto" 的对话 (需在 client 层补 tools 参数透传) …
env = run_tool_call(tc.name, tc.arguments_json)   # -> 统一信封
```

### 5.2 stdio MCP 服务 (外部客户端)

```bash
# 启动 (项目根目录; core/config 会自动向上定位 config.yaml/.env)
.venv\Scripts\python.exe -m mcp.server
```

客户端配置示例 (任何支持 stdio MCP 的宿主):

```json
{
  "mcpServers": {
    "alpha-radar": {
      "command": "C:/Users/GluttonousCat/PycharmProjects/ai-assistant/.venv/Scripts/python.exe",
      "args": ["-m", "mcp.server"],
      "cwd": "C:/Users/GluttonousCat/PycharmProjects/ai-assistant"
    }
  }
}
```

协议支持: `initialize` / `notifications/initialized` / `ping` / `tools/list` / `tools/call`,
protocolVersion `2024-11-05`, 零第三方依赖 (手写 JSON-RPC, UTF-8)。
工具执行期间的底座 print 输出 (如 range_trading 扫描排名) 被强制重定向到 stderr,
保证 stdout 只有 JSON-RPC 帧。

## 六、验证记录

| 日期 | 内容 | 结果 |
|------|------|------|
| 2026-09-07 | fast 档冒烟 (18 工具 21 用例, `scripts/smoke_mcp_tools.py`) | 全部 PASS, 单工具 0ms~6s |
| 2026-09-07 | stdio 协议端到端 (initialize/list/call/错误路径) | PASS, isError 正确标记 |
| 2026-09-07 | verify_forecasts 抽查: 阳光电源 2025 净利预测 135亿 vs 实际 134.6亿, err -0.3% 判「兑现」 | 数值正确 |
| 2026-09-07 | detect_financial_risk 抽查: 中际旭创命中「经营现金流仅为净利润 13%」medium | 符合预期 |
| 2026-09-07 | heavy 档 (全市场扫描 + forge): regime 0.5s / range 578s / trend 365s / setup 172s / forge 509s (含 2 次 LLM 超时重试后成功) / extract 不存在文件→正确报 ToolError | 全部 PASS |
| 2026-09-07 | pytest mcp/tests (注册表完整性/信封契约/schema 规范) | 24 passed |

已知限制 (审核时留意):
1. **`mcp` 包名与官方 MCP SDK 同名**: 若日后 `pip install mcp`, 从项目根运行会 import 到本目录。
   如需共存可整体改名 `mcptools/` (内部引用只有 `mcp.` 前缀, 机械替换即可)。
2. verify_forecasts 现库可回验样本少: forecast 表仅 252 行且多为 2026E+ 期间/外币口径
   (中芯国际的预测全是美元)。随研报持续入库自动改善。
3. run_quant_scan 的 range/trend/setup 为全市场重扫描 (分钟级); regime 秒级。
   Agent 侧建议对重工具做耗时提示。
4. read_report 带 question 时内部走 LLM (ReportSkill qa), 首次调用有冷启动延迟。
5. forge_chain 为多轮 LLM 生成 (1-3 分钟), 产物是 YAML 草稿, **不自动写文件** (save 由人工执行)。

## 七、接入状态 (2026-09-07 已实施)

1. ✅ `agent/loop.py` — LLM + `openai_tools()` 工具循环 (≤8 轮), 沙盒 (只读/超时/防重复), 见 docs/agents/agent_loop.md
2. ✅ `llm/client.py` — 新增 `invoke_with_tools` (deepseek function-calling)
3. ✅ `api/finance.py` — 新端点 `POST /api/v1/agent/stream` (stage/tool_call/data/delta 事件)
4. ✅ `agent/history_store.py` — 会话历史 (多轮追问); 前端 AgentChat 已切换并携带 session_id
5. ✅ `mcp/server.py` — stdio 服务默认只读模式 (`ALPHA_MCP_READ_ONLY=1`, 写类工具不可见不可调)

E2E 实测 (用户原例): T1「中际旭创2025年报净利润」→ 107.97亿;
T2「环比竟然下降了，数据对吗？」(零线索追问) → 继承标的 → 重新取数拆单季 →
「单季逐季抬升, 没有环比下降, 是累计口径假象; 同比+109%」。
