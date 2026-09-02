> **[历史快照 2026-08-15]** 本文档为 v1.0 研发启动稿。此后架构已显著演进
> （登录鉴权/邀请码、LLM 用途路由、扫描件 Agent、域名部署、逐文件入库等），
> 当前实态以根目录 [AGENTS.md](../AGENTS.md) 为准；本文保留供追溯产品初心与边界。

# 智能财务分析 Copilot — 产品需求与技术研发文档

**版本**: v1.0
**日期**: 2026-08-15
**状态**: 研发启动稿
**所属项目**: ai-assistant（智能投研助手）

---

## 目录

- [第一部分 产品需求文档（PRD）](#第一部分-产品需求文档prd)
  - [1. 产品定位与愿景](#1-产品定位与愿景)
  - [2. 目标用户与场景](#2-目标用户与场景)
  - [3. 核心功能需求](#3-核心功能需求)
  - [4. 非功能需求](#4-非功能需求)
  - [5. 边界与排除项](#5-边界与排除项)
- [第二部分 技术研发文档](#第二部分-技术研发文档)
  - [1. 系统架构](#1-系统架构)
  - [2. 数据架构与表设计](#2-数据架构与表设计)
  - [3. Agent 与 Skill 设计](#3-agent-与-skill-设计)
  - [4. 关键技术实现](#4-关键技术实现)
  - [5. 接口设计](#5-接口设计)
  - [6. 研发路线图](#6-研发路线图)
  - [7. 测试与评估](#7-测试与评估)
- [第三部分 落地建议](#第三部分-落地建议)

---

## 第一部分 产品需求文档（PRD）

### 1. 产品定位与愿景

**一句话定位**：面向财务分析师与投研人员的 AI 数据分析 Copilot——通过自然语言完成对上市公司财务、行情、研报数据的复杂查询、对比与异常检测。

**要解决的问题**：
- 财务分析门槛高：专业人员需要手写 SQL 或在 Excel 中反复取数
- 数据孤岛：财务数据、行情数据、研报文本分散，无法交叉验证
- 异常识别滞后：传统依赖人工阅读财报发现风险，效率低、易遗漏

**愿景**：成为投研人员的"数据外挂"——让懂业务的人不用懂技术，直接问数据库要答案。

### 2. 目标用户与场景

| 用户角色 | 核心诉求 | 使用场景 |
|---------|---------|---------|
| 券商研究员 | 快速对比多公司财务指标，撰写研报初稿 | 横向行业对比、深度个股分析 |
| 审计/尽调人员 | 识别财务报表异常信号，降低踩雷风险 | 财务异常检测、一致性校验 |
| 投资经理 | 验证投资逻辑，监控持仓基本面变化 | 基本面追踪、季报解读 |
| 量化研究员 | 批量提取结构化财务因子 | 因子计算、数据清洗 |

### 3. 核心功能需求

#### F1：自然语言财务查询（Text-to-SQL）— P0

用户通过自然语言查询结构化财务数据。

**需求描述**：
- 支持中文自然语言查询财务三表、行情数据、估值指标
- 支持复杂条件组合（多条件筛选、时间范围、行业过滤）
- 返回结构化表格 + 自然语言解读

**典型查询**：
```
- "查询 2023 年 ROE 大于 20% 且资产负债率小于 40% 的所有股票"
- "对比贵州茅台、五粮液、泸州老窖最近三年的营收和净利润增速"
- "找出 2024Q1 净利润同比增长但经营现金流同比下滑的公司"
```

**验收标准**：
- 简单查询（单表单条件）准确率 ≥ 95%
- 复杂查询（多表 join、聚合）准确率 ≥ 85%
- 查询响应时间 ≤ 5 秒

#### F2：多维度财务对比分析 — P0

跨公司、跨期、跨表的多维对比分析。

**需求描述**：
- 支持多股票财务指标横向对比（最多 10 只）
- 支持单股票多年纵向趋势分析
- 自动生成对比图表与文字摘要
- 支持自定义指标组合

**典型场景**：
```
用户: "对比 5 家白酒公司 2022-2024 年报的盈利质量"
系统:
  - 拉取 5 家公司 fin.income / fin.fina_indicator 数据
  - 计算毛利率、净利率、ROE、经营现金流/净利润比值
  - 生成对比表格 + 趋势图 + LLM 分析结论
```

#### F3：财务异常智能检测 — P1

自动识别财务报表中的风险信号与勾稽异常。

**需求描述**：
- 预置 10+ 种典型异常模式（基于财务勾稽关系）
- 支持单股票扫描与全市场批量扫描
- 输出异常类型、置信度、解释说明

**内置检测规则**（初版）：

| 规则编号 | 异常模式 | 检测逻辑 | 风险等级 |
|---------|---------|---------|---------|
| R01 | 利润增长但现金流恶化 | 净利润同比 >10% 且经营现金流同比 <-10% | 高 |
| R02 | 应收账款增速远超营收 | 应收账款增速 > 营收增速 × 2 | 中 |
| R03 | 存货异常堆积 | 存货增速 > 营收增速 × 1.5 且存货周转率下降 | 中 |
| R04 | 商誉占比过高 | 商誉/净资产 > 30% | 高 |
| R05 | 毛利率显著高于同行 | 毛利率 > 行业均值 + 2 倍标准差 | 中 |
| R06 | 经营现金流持续为负 | 连续 3 年经营现金流为负但净利润为正 | 高 |
| R07 | 短期偿债压力大 | 流动比率 < 1 且速动比率 < 0.5 | 高 |
| R08 | 财务费用异常波动 | 财务费用同比变化 > 100% | 低 |

#### F4：研报文本一致性校验 — P1

将知识星球爬取的研报文本与结构化财务数据进行交叉验证。

**需求描述**：
- 上传研报文本（或从知识星球自动获取）
- 提取研报中的关键财务预测/结论（营收预测、净利润预测、目标价）
- 与数据库中的实际财务数据对比
- 输出一致性评分与偏差说明

**典型场景**：
```
研报预测: "预计 2024 年营收 1500 亿，同比增长 15%"
实际数据: fin.income 显示 2024 年营收 1420 亿，同比增长 10.2%
系统输出: 偏差 -5.3%，研报高估 80 亿
```

#### F5：智能分析报告生成 — P2

给定股票代码，自动生成结构化分析报告。

**报告大纲**（可配置）：
1. 公司概况（业务、行业地位）
2. 财务三表核心数据摘要
3. 关键财务指标趋势（ROE、毛利率、周转率等）
4. 估值水平（PE/PB/PS 历史分位、行业对比）
5. 风险信号检测（异常规则命中情况）
6. 综合结论与建议

### 4. 非功能需求

| 维度 | 需求 |
|-----|------|
| 性能 | 单查询 ≤5s，复杂分析 ≤30s，报告生成 ≤60s |
| 可用性 | 工作时段可用性 ≥99%，数据更新延迟 ≤24h |
| 准确性 | 数据准确率 100%（源于 Tushare），SQL 生成准确率 ≥85% |
| 可扩展性 | 支持新增财务规则、新增数据源（如宏观数据） |
| 安全性 | API 密钥隔离、SQL 注入防护、查询审计日志 |

### 5. 边界与排除项

**明确不做**：
- 实时行情推送（仅 T+1 数据）
- 自动交易/荐股建议（纯分析工具，不给出买卖指令）
- 期货、期权、外汇等非 A 股标的
- 北交所、ST 股、B 股（已在数据源层面排除）

---

## 第二部分 技术研发文档

### 1. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         接入层 (API)                             │
│  FastAPI Router  │  WebSocket (实时流式)  │  CLI (交互式)         │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│                      Agent 编排层 (大脑)                          │
│  LangGraph StateGraph                                            │
│  ├── 意图识别节点 (LLM Router)                                   │
│  ├── 任务规划节点 (Task Planner)                                 │
│  └── 多 Agent 协同 (Supervisor)                                  │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│                       Skill 层 (领域方法论)                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ 财务查询  │ │ 对比分析  │ │ 异常检测  │ │ 研报校验  │           │
│  │  Skill   │ │  Skill   │ │  Skill   │ │  Skill   │           │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘           │
└───────┼────────────┼────────────┼────────────┼──────────────────┘
        │            │            │            │
┌───────▼────────────▼────────────▼────────────▼──────────────────┐
│                       Tool 层 (原子能力)                          │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐           │
│  │ SQL 生成 │ │ 数据查询  │ │ 指标计算  │ │ 文本提取  │           │
│  │  Tool    │ │  Tool    │ │  Tool    │ │  Tool    │           │
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘           │
└────────────────────────┬────────────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────────────┐
│                      数据层 (Storage)                             │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────┐  │
│  │   PostgreSQL     │  │   SQLite         │  │   MySQL      │  │
│  │  stock schema    │  │  知识星球爬虫     │  │  遗留业务库   │  │
│  │  fin schema      │  │  (研报/文件)      │  │              │  │
│  └──────────────────┘  └──────────────────┘  └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### 2. 数据架构与表设计

#### 2.1 已有数据资产（复用）

| Schema | 表 | 说明 | 数据量 |
|--------|-----|------|--------|
| stock | stock_basic | 股票池（主板/创业板/科创板） | ~5,000 |
| stock | trade_calendar | 交易日历 | ~10,000 |
| stock | daily | 日K行情 | ~3,000万（全量后） |
| stock | adj_factor | 复权因子 | ~3,000万 |
| stock | daily_basic | 每日估值 | ~3,000万 |
| fin | income | 利润表 | ~50万（5000股×100期） |
| fin | balancesheet | 资产负债表 | ~50万 |
| fin | cashflow | 现金流量表 | ~50万 |
| fin | fina_indicator | 财务指标 | ~50万 |

#### 2.2 新增表设计

**2.2.1 研报元数据表**（`fin.report_meta`）

```sql
CREATE TABLE fin.report_meta (
    report_id       SERIAL PRIMARY KEY,
    ts_code         VARCHAR(16) NOT NULL,      -- 标的股票
    title           VARCHAR(256),              -- 研报标题
    author          VARCHAR(64),               -- 分析师
    org_name        VARCHAR(64),               -- 券商名称
    publish_date    DATE,                      -- 发布日期
    report_type     VARCHAR(16),               -- 研报类型（深度/点评/季报）
    source          VARCHAR(32),               -- 来源（知识星球/手动上传）
    file_path       TEXT,                      -- 原始文件路径
    content_text    TEXT,                      -- 提取的文本内容
    extraction_status VARCHAR(16) DEFAULT 'pending', -- pending/extracted/verified
    created_at      TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.report_meta IS '研报元数据与文本内容';
```

**2.2.2 研报预测提取表**（`fin.report_forecast`）

```sql
CREATE TABLE fin.report_forecast (
    id              SERIAL PRIMARY KEY,
    report_id       INTEGER REFERENCES fin.report_meta(report_id),
    ts_code         VARCHAR(16) NOT NULL,
    forecast_type   VARCHAR(32),               -- 预测类型：营收/净利润/EPS/目标价
    forecast_period VARCHAR(16),               -- 预测期间：2024/2025E
    forecast_value  NUMERIC(24,4),             -- 预测值
    forecast_unit   VARCHAR(16),               -- 单位：亿元/元
    confidence      NUMERIC(5,2),              -- 提取置信度 0-100
    raw_text        TEXT,                      -- 原始文本片段
    created_at      TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.report_forecast IS '研报关键预测数据提取结果';
```

**2.2.3 异常检测规则表**（`fin.anomaly_rules`）

```sql
CREATE TABLE fin.anomaly_rules (
    rule_id         VARCHAR(16) PRIMARY KEY,   -- R01/R02...
    rule_name       VARCHAR(64) NOT NULL,
    description     TEXT,
    sql_template    TEXT NOT NULL,             -- 检测 SQL 模板
    risk_level      VARCHAR(8) NOT NULL,       -- high/medium/low
    enabled         BOOLEAN DEFAULT TRUE,
    created_at      TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.anomaly_rules IS '财务异常检测规则配置';
```

**2.2.4 异常检测结果表**（`fin.anomaly_results`）

```sql
CREATE TABLE fin.anomaly_results (
    id              SERIAL PRIMARY KEY,
    ts_code         VARCHAR(16) NOT NULL,
    rule_id         VARCHAR(16) REFERENCES fin.anomaly_rules(rule_id),
    period          VARCHAR(16),               -- 检测期间
    anomaly_score   NUMERIC(5,2),              -- 异常分数 0-100
    detail          JSONB,                     -- 详细数据（触发值、阈值等）
    detected_at     TIMESTAMP DEFAULT now()
);
CREATE INDEX idx_anomaly_ts_code ON fin.anomaly_results(ts_code);
COMMENT ON TABLE fin.anomaly_results IS '财务异常检测结果记录';
```

**2.2.5 查询日志表**（`fin.query_log`）

```sql
CREATE TABLE fin.query_log (
    id              SERIAL PRIMARY KEY,
    session_id      VARCHAR(64),
    user_input      TEXT NOT NULL,
    generated_sql   TEXT,
    sql_valid       BOOLEAN,
    execution_time_ms INTEGER,
    row_count       INTEGER,
    skill_used      VARCHAR(32),               -- 命中的 Skill
    error_msg       TEXT,
    created_at      TIMESTAMP DEFAULT now()
);
COMMENT ON TABLE fin.query_log IS 'ChatBI 查询审计日志';
```

#### 2.3 数据库视图（简化 Agent 查询）

**2.3.1 核心财务指标宽表**

```sql
CREATE OR REPLACE VIEW fin.v_financial_summary AS
SELECT
    i.ts_code,
    i.end_date,
    i.ann_date,
    i.total_revenue,
    i.revenue,
    i.operate_profit,
    i.total_profit,
    i.n_income,
    i.n_income_attr_p,
    i.basic_eps,
    i.rd_exp,
    b.total_assets,
    b.total_liab,
    b.equity_attr_p,
    b.money_cap,
    b.accounts_receiv,
    b.invent,
    b.goodwill,
    c.n_cashflow_act,
    c.n_cashflow_inv_act,
    c.n_cash_flows_fnc_act,
    f.roe,
    f.roe_waa,
    f.netprofit_yoy,
    f.or_yoy,
    f.grossprofit_margin,
    f.netprofit_margin,
    f.debt_to_assets,
    f.current_ratio,
    f.quick_ratio
FROM fin.income i
LEFT JOIN fin.balancesheet b
    ON i.ts_code = b.ts_code AND i.end_date = b.end_date AND i.report_type = b.report_type
LEFT JOIN fin.cashflow c
    ON i.ts_code = c.ts_code AND i.end_date = c.end_date AND i.report_type = c.report_type
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
WHERE i.report_type = '1';
COMMENT ON VIEW fin.v_financial_summary IS '核心财务指标宽表（合并报表）';
```

**2.3.2 行情+估值日频宽表**

```sql
CREATE OR REPLACE VIEW stock.v_daily_valuation AS
SELECT
    d.trade_date,
    d.ts_code,
    s.name,
    s.industry,
    d.open, d.high, d.low, d.close, d.vol, d.amount, d.pct_chg,
    db.close AS close_price,
    db.turnover_rate,
    db.pe_ttm,
    db.pb,
    db.ps_ttm,
    db.total_mv,
    db.circ_mv,
    d.adj_factor,
    d.close * d.adj_factor AS close_adj  -- 后复权收盘价
FROM stock.daily d
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
LEFT JOIN stock.stock_basic s
    ON d.ts_code = s.ts_code;
COMMENT ON VIEW stock.v_daily_valuation IS '日频行情+估值宽表';
```

### 3. Agent 与 Skill 设计

#### 3.1 Agent 状态定义（扩展 `agent/state.py`）

```python
@dataclass
class FinancialAgentState(AgentState):
    """财务分析 Agent 状态"""
    # 意图识别结果
    intent_type: Optional[str] = None      # query/compare/detect/report/verify
    intent_confidence: float = 0.0

    # 查询参数
    target_codes: List[str] = field(default_factory=list)   # 目标股票
    metrics: List[str] = field(default_factory=list)         # 查询指标
    time_range: Optional[Tuple[str, str]] = None             # 时间范围
    conditions: Dict[str, Any] = field(default_factory=dict) # 筛选条件

    # SQL 生成
    generated_sql: Optional[str] = None
    sql_valid: bool = False

    # 执行结果
    query_result: Optional[pd.DataFrame] = None
    result_summary: str = ""

    # 异常检测
    anomaly_hits: List[Dict] = field(default_factory=list)

    # 研报校验
    report_text: str = ""
    forecast_extracted: List[Dict] = field(default_factory=list)
    verification_result: Optional[Dict] = None
```

#### 3.2 主图设计（`agent/graph.py` 扩展）

```python
def create_financial_agent():
    """创建财务分析 Agent 工作流"""
    workflow = StateGraph(FinancialAgentState)

    # 节点
    workflow.add_node("parse_intent", parse_financial_intent_node)
    workflow.add_node("plan_task", plan_financial_task_node)
    workflow.add_node("generate_sql", generate_sql_node)
    workflow.add_node("validate_sql", validate_sql_node)
    workflow.add_node("execute_query", execute_query_node)
    workflow.add_node("analyze_result", analyze_result_node)
    workflow.add_node("detect_anomaly", detect_anomaly_node)
    workflow.add_node("verify_report", verify_report_node)
    workflow.add_node("generate_response", generate_response_node)

    # 入口
    workflow.set_entry_point("parse_intent")

    # 意图路由
    workflow.add_conditional_edges(
        "parse_intent",
        route_by_intent,
        {
            "query": "plan_task",
            "compare": "plan_task",
            "detect": "detect_anomaly",
            "verify": "verify_report",
            "unknown": "generate_response",
        }
    )

    # SQL 生成与执行流程
    workflow.add_edge("plan_task", "generate_sql")
    workflow.add_edge("generate_sql", "validate_sql")
    workflow.add_conditional_edges(
        "validate_sql",
        route_sql_valid,
        {"valid": "execute_query", "invalid": "generate_sql"}  # 重试循环
    )
    workflow.add_edge("execute_query", "analyze_result")
    workflow.add_edge("analyze_result", "generate_response")
    workflow.add_edge("detect_anomaly", "generate_response")
    workflow.add_edge("verify_report", "generate_response")
    workflow.add_edge("generate_response", END)

    return workflow.compile()
```

#### 3.3 核心 Skill 实现

**3.3.1 财务查询 Skill**（`skills/fin_query/skill.py`）

核心逻辑：
1. **Schema Linking**：将用户查询中的实体（股票名、指标名、时间词）映射到数据库 schema
2. **SQL 生成**：基于 LLM 生成 PostgreSQL 查询
3. **SQL 校验**：AST 解析 + 只读检查 + 危险操作拦截
4. **执行与解释**：执行 SQL，LLM 解读结果

```python
class FinancialQuerySkill(BaseSkill):
    name = "fin_query"
    description = "自然语言查询财务与行情数据"
    intent_keywords = ["查询", "查一下", "多少", "对比", "排名", "筛选"]

    def run(self, context: SkillContext) -> SkillContext:
        # 1. 提取实体
        entities = extract_entities(context.user_input)
        # 2. 生成 SQL
        sql = self._generate_sql(entities)
        # 3. 校验
        if not self._validate_sql(sql):
            context.error = "SQL 生成失败或包含危险操作"
            return context
        # 4. 执行
        result = self._execute_sql(sql)
        # 5. 解读
        context.result = self._interpret_result(result)
        return context
```

**3.3.2 异常检测 Skill**（`skills/anomaly_detect/skill.py`）

```python
class AnomalyDetectSkill(BaseSkill):
    name = "anomaly_detect"
    description = "财务异常信号检测"
    intent_keywords = ["风险", "异常", "预警", "检测", "排雷"]

    def run(self, context: SkillContext) -> SkillContext:
        codes = context.params.get("codes", [])
        rules = context.params.get("rules", ["R01","R02","R03","R04","R05","R06"])

        results = []
        for code in codes:
            for rule_id in rules:
                hit = self._check_rule(code, rule_id)
                if hit:
                    results.append(hit)

        context.result = self._format_anomalies(results)
        return context

    def _check_rule(self, ts_code: str, rule_id: str) -> Optional[Dict]:
        # 从 fin.anomaly_rules 读取 SQL 模板
        # 替换参数执行
        # 返回异常详情或 None
```

**3.3.3 研报校验 Skill**（`skills/report_verify/skill.py`）

```python
class ReportVerifySkill(BaseSkill):
    name = "report_verify"
    description = "研报预测与实际数据一致性校验"
    intent_keywords = ["校验", "验证", "研报", "预测", "一致性"]

    def run(self, context: SkillContext) -> SkillContext:
        report_text = context.params.get("report_text")
        ts_code = context.params.get("ts_code")

        # 1. LLM 提取预测数据
        forecasts = self._extract_forecasts(report_text, ts_code)
        # 2. 查实际数据
        actuals = self._fetch_actuals(ts_code, forecasts)
        # 3. 对比计算偏差
        verification = self._compare(forecasts, actuals)

        context.result = verification
        return context
```

#### 3.4 Prompt 设计（关键资产）

**Schema Linking Prompt**（`skills/fin_query/prompts.py`）：

```
你是一个财务数据查询助手。请将用户查询映射到数据库 schema。

可用表与关键字段:
{schema_info}

用户查询: {user_input}

请提取以下信息并以 JSON 返回:
{{
    "stocks": ["股票代码或名称列表"],
    "metrics": ["查询的财务指标"],
    "time_range": {{"start": "开始时间", "end": "结束时间", "period": "报告期"}},
    "conditions": ["筛选条件"],
    "query_type": "point|range|compare|rank"
}}

示例:
输入: "查询 2023 年 ROE 大于 20% 的股票"
输出: {{
    "stocks": [],
    "metrics": ["roe"],
    "time_range": {{"period": "2023"}},
    "conditions": ["roe > 20"],
    "query_type": "rank"
}}
```

**SQL 生成 Prompt**：

```
基于以下信息生成 PostgreSQL 查询。

Schema:
- fin.income: ts_code, end_date, report_type, revenue, n_income_attr_p...
- fin.fina_indicator: ts_code, end_date, roe, netprofit_yoy...
- fin.balancesheet: ts_code, end_date, total_assets, goodwill...
- stock.daily: trade_date, ts_code, close, pct_chg...
- stock.daily_basic: trade_date, ts_code, pe_ttm, pb, total_mv...
- stock.stock_basic: ts_code, name, industry, market...

查询需求: {extracted_intent}

要求:
1. 只生成 SELECT 查询，禁止 INSERT/UPDATE/DELETE/DROP
2. 使用表别名，字段明确
3. 时间字段统一为 end_date（财报）或 trade_date（行情）
4. 限制返回行数（LIMIT 1000）
5. 包含必要的 WHERE 条件

返回格式:
{{
    "sql": "SELECT ...",
    "explanation": "查询逻辑说明",
    "tables_used": ["表1", "表2"]
}}
```

### 4. 关键技术实现

#### 4.1 Text-to-SQL 引擎

**流程**：

```
用户输入
  → 意图识别（LLM Router）
  → 实体提取（Schema Linking）
  → SQL 生成（LLM + Few-shot）
  → SQL 校验（SQLGlot AST 解析）
  → 执行（只读连接）
  → 结果解读（LLM）
```

**SQL 校验关键代码**：

```python
import sqlglot

def validate_sql(sql: str) -> tuple[bool, str]:
    """校验 SQL 安全性"""
    try:
        parsed = sqlglot.parse_one(sql, dialect="postgres")
        # 只允许 SELECT
        if parsed.key != "select":
            return False, "仅允许 SELECT 查询"
        # 禁止危险函数
        for func in parsed.find_all(sqlglot.exp.Func):
            if func.name.upper() in ("PG_SLEEP", "XP_CMDSHELL", "LOAD_FILE"):
                return False, f"禁止函数: {func.name}"
        return True, ""
    except Exception as e:
        return False, f"SQL 解析失败: {e}"
```

#### 4.2 多 Agent 协同（对比分析场景）

```
用户: "对比 5 家白酒公司盈利质量"
  ↓
Supervisor Agent（任务规划）
  ├─ 子任务1: 数据提取 Agent → 拉取 5 家公司 3 年财务数据
  ├─ 子任务2: 指标计算 Agent → 计算盈利质量指标（毛利率/ROE/现金流比）
  ├─ 子任务3: 对比分析 Agent → 横向对比 + 排名
  └─ 子任务4: 报告生成 Agent → 汇总成结构化报告
```

#### 4.3 研报文本提取（LLM + 规则）

**两阶段提取**：
1. **LLM 粗提取**：从研报全文提取所有数字预测
2. **规则精校验**：正则匹配 + 单位归一化（亿/万/元统一）

```python
FORECAST_PATTERNS = [
    (r"预计.{0,10}?(\d{4}).{0,10}?营收.{0,10}?([\d.]+).{0,5}?亿", "revenue"),
    (r"预计.{0,10}?(\d{4}).{0,10}?净利润.{0,10}?([\d.]+).{0,5}?亿", "net_profit"),
    (r"目标价.{0,10}?([\d.]+).{0,5}?元", "target_price"),
]
```

### 5. 接口设计

#### 5.1 REST API

| 方法 | 路径 | 说明 | 请求体 |
|-----|------|------|--------|
| POST | /api/v1/query | 自然语言查询 | `{"text": "...", "session_id": "..."}` |
| POST | /api/v1/compare | 多股票对比 | `{"codes": [...], "metrics": [...], "period": "..."}` |
| POST | /api/v1/detect | 异常检测 | `{"codes": [...], "rules": [...]}` |
| POST | /api/v1/verify | 研报校验 | `{"ts_code": "...", "report_text": "..."}` |
| GET  | /api/v1/report/{ts_code} | 生成分析报告 | - |
| GET  | /api/v1/schema | 获取数据字典 | - |

#### 5.2 WebSocket（流式）

```
ws://host:port/ws/{session_id}

消息格式:
Client → Server: {"type": "query", "text": "查询贵州茅台2023年营收"}
Server → Client: {"type": "status", "stage": "generating_sql"}
Server → Client: {"type": "sql", "content": "SELECT ..."}
Server → Client: {"type": "result", "data": [...], "summary": "..."}
```

### 6. 研发路线图

| 阶段 | 目标 | 交付物 | 工期 |
|-----|------|--------|------|
| **M1** | Text-to-SQL 核心链路 | 财务查询 Skill + SQL 校验器 + 基础 API | 2 周 |
| **M2** | 多维度对比分析 | 对比分析 Skill + 图表生成 + 多 Agent 协同 | 2 周 |
| **M3** | 异常检测引擎 | 10 条规则 + 检测 Skill + 批量扫描 API | 1.5 周 |
| **M4** | 研报校验 | 文本提取 + 一致性对比 + 知识星球集成 | 1.5 周 |
| **M5** | 报告生成 + 优化 | 报告 Skill + Prompt 优化 + 准确率提升 | 2 周 |

### 7. 测试与评估

**SQL 生成准确率测试集**：
- 构建 200 条人工标注的「自然语言 → SQL」测试对
- 按难度分级：简单（单表单条件）、中等（多表 join）、复杂（嵌套子查询）
- 目标：简单 95%、中等 85%、复杂 70%

**异常检测有效性验证**：
- 回溯历史财务造假案例（如康美药业、獐子岛）
- 验证规则能否提前识别
- 统计误报率与漏报率

---

## 第三部分 落地建议

**本周立即可做**：
1. 等行情/财务全量回填完成（后台运行中）
2. 实现 `v_financial_summary` 视图 + SQL 校验器
3. 写第一版 `FinancialQuerySkill`，跑通"查询平安银行 2023 年营收"端到端

**求职话术准备**：
- "这个项目解决的是**企业财务数据的民主化访问**问题，让业务人员不用学 SQL 就能做复杂分析"
- "技术上挑战在于 **Schema Linking 的准确性** 和 **多表关联的推理**——我用 LangGraph 做了多 Agent 协同来解决"
- "数据规模是 5000+ 上市公司、10+ 张关联表、3000 万+ 行情记录"
