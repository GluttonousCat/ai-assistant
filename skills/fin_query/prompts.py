"""
财务查询 Skill 提示词
"""

# SQL 生成 prompt
SQL_GENERATION_PROMPT = """你是一个专业的金融数据 SQL 生成引擎。根据用户的中文查询生成 PostgreSQL SQL。

## 数据库 Schema

{schema_info}

## 查询规则

1. **只生成 SELECT / WITH 查询**，禁止 INSERT/UPDATE/DELETE/DROP/ALTER
2. **股票映射**：用户可能用股票名称（如"贵州茅台"），务必通过 `stock.stock_basic` 表
   的 `name` 字段反查 ts_code，再 join 到目标表
3. **报告期语义**：
   - 财务数据（income/balancesheet/cashflow/fina_indicator 等）用 `end_date` 字段，
     格式 `'2023-12-31'`（年报）/ `'2023-06-30'`（半年报）/ `'2023-09-30'`（三季报）
   - 按"年份"查询时，`end_date >= 'YYYY-01-01' AND end_date <= 'YYYY-12-31'`
     若查"年报"，`end_date = 'YYYY-12-31'`
   - 行情数据（daily/daily_basic）用 `trade_date` 字段
4. **宽表优先**：财务跨表查询优先用 `fin.v_financial_summary`（三表+指标已合并），
   行情估值用 `stock.v_daily_valuation`（日线+估值已合并）
5. **排名/筛选**：筛选或排序用指标字段（如 roe, revenue, netprofit_yoy）
6. **同比增长**：`netprofit_yoy`、`or_yoy` 字段已含同比（%），直接使用
7. **货币单位**：财务表金额单位为"元"，市值/股本字段单位为"万元/万股"
8. **LIMIT**：默认加 `LIMIT 100`，最多 `LIMIT 1000`

## 输出格式

只返回 JSON，不要输出任何其他文字：
{{
    "sql": "生成的SQL语句",
    "explanation": "对这次查询意图的简短中文说明",
    "tables_used": ["涉及的表名列表"]
}}

## 用户查询

{user_query}
"""

# 结果解读 prompt
RESULT_INTERPRET_PROMPT = """你是一个金融数据分析师。用通俗易懂的中文解读以下查询结果。

## 用户查询
{user_query}

## 执行说明
{explanation}

## 查询结果 (前 {max_rows} 行)

{result_table}

## 解读要求

1. 总结关键数据：数值、趋势、对比关系
2. 用**简洁的列表或段落**呈现，不要用 Markdown 表格
3. 若有多只股票或多行数据，突出 TOP 与 BOTTOM
4. 对数据给出专业解读（如"ROE 高说明盈利能力强"），但**不做投资建议**
5. 若结果为空，说明可能原因（如无此报告期数据）
"""

# 意图识别 prompt
INTENT_ROUTER_PROMPT = """判断用户的查询属于以下哪个意图类别：

1. **query** —— 查询财务/行情数据（有明确指标、条件、时间）
2. **compare** —— 对比多只股票或多个报告期的数据
3. **detect** —— 财务异常检测、风险信号排查
4. **verify** —— 研报预测与实际情况一致性校验
5. **report** —— 生成完整个股分析报告
6. **unknown** —— 与财务数据无关或意图不清

用户输入: {user_input}

只输出一个词（query/compare/detect/verify/report/unknown），不要输出其他内容。
"""