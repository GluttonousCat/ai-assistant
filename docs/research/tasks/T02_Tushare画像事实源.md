# T02 · Tushare/画像事实源 (Stage 1)

> 先读 T00。你独占: skills/research/sources/tushare_source.py (+ sources/__init__.py)

## 目标
把"画像八板块 + 关键财务时序"转成 Fact 证据分片 facts_tushare.json。

## 输入 / 输出
- 输入: 股票名或代码 (如 "贵州茅台"), 年数 (默认 5)
- 输出: EvidenceShard(kind="tushare") → output/research/<ts_code>/facts_tushare.json
- 函数签名 (供 T06/T08 复用):
  `def collect_tushare_facts(stock: str, years: int = 5, out_dir: Path | None = None) -> EvidenceShard`

## 实现要点
1. 数据获取走**现有能力**, 禁直写 SQL:
   - `skills/content/profile.py` 的 `build_company_profile(stock, years)` — 八板块画像
     (主营/财务/估值/行业/股东等), 只 import 不修改
   - 需补时序时经 harness.mcp 注册表调工具:
     `from harness.mcp import get_registry; get_registry().call("query_financials", {...})`
2. 每条 Fact: eid 如 `T:profile.mainbiz.2025A` / `T:tushare.roe.2024A`; source 精确到
   板块名或 工具名+参数; 数值进 value+unit; period 用 2021A..2025A 口径。
   注意财报**累计口径** (环比需换算时在 source 注明公式, confidence=0.9)。
3. 覆盖度: 财务三表关键指标 5 年时序 + 主营构成 + 估值水位 + 股东/行业归属, 目标 ≥60 条。
4. meta 记录生成时间与覆盖说明。CLI 可直跑:
   `python -m skills.research.sources.tushare_source 贵州茅台`

## 验收
茅台跑通; facts ≥60, 每条 source+period 非空; 抽 5 条能人工回溯到数据来源。

## 禁止
改 profile.py; 动 storage/; 混入任何研报观点 (那是 T04 的事)。
