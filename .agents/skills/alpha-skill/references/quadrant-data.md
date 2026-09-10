# 四象限取数细节（alpha-skill）

实现集中在 `beta_alpha/skills/alpha.py`（`_load_forecast_divergence` / `_load_momentum` / `_load_valuation` / `_load_views`，纯函数 `to_yuan`/`classify_drift` 有单元测试在 `beta_alpha/tests/`），改判定先读本文。

## 象限 1：预测分歧（fin.report_forecast × fin.report_meta）

- 取 `report_forecast` 按 `forecast_type + forecast_period` 分组，仅保留 ≥2 份预测的组
- **单位归一**：`forecast_unit` 映射到元（亿元 1e8 / 万亿 1e12 / 万 1e4 / 百万 1e6；`%`、`倍` 不换算），映射表 `_UNIT_FACTOR`——混合单位直接求均值会得出荒谬 CV，这是本象限最容易踩的坑
- `cv = std/|mean|`（样本标准差）；方向 = 按发布时间排序前后半段均值对比，±3% 内算持平
- 覆盖少的原因：`report_forecast.ts_code` 只有研报元数据提取成功绑定代码时才非空（当前约 1/6），海外研报标的多为美股/港股代码

## 象限 2：财务动量（fin.fina_indicator）

- `report_type='1'`（合并报表），近 8 个报告期，按 `end_date` 升序看边际
- 阈值：or_yoy / netprofit_yoy 相邻期差 ≥+5pp 提速、≤-5pp 降速；毛利率近 2 期均值 vs 之前均值定上行/下行
- 注意三表是**报告期累计值**，单季只有 `q_` 前缀字段（`q_sales_yoy` 已在输出里）

## 象限 3：估值分位（stock.daily_basic）

- 窗口 `current_date - 1095`（3 年），`pe_ttm`/`pb` 取 >0 值，分位 = 低于现值的天数占比；样本 <60 天返回 null（次新股）
- PG NUMERIC 经 psycopg2 返回 Decimal——pandas 运算前必须 `astype(float)`（本项目历史坑，chain_analysis 也一样）

## 象限 4：研报观点（fin.report_meta）

- `ts_code = X OR title ILIKE %名称%`，近 5 篇；评级与核心观点从 `analysis_json`（JSON 文本）宽容解析，缺失字段给 null 不报错

## 平台内 vs ZCode 内

同一实现被两处消费：平台对话页（`beta_alpha/streaming.py` 的 `stream_alpha`，api 只做薄包装）与本模块的 CLI（`python -m agent.beta_alpha.skills.alpha`）。取数逻辑只改 `beta_alpha/skills/alpha.py` 的四个 `_load_*`；前端平表由 `AlphaSkill.build_table` 单点生成，两处共用，无镜像代码。

## 意图触发词

平台侧：`agent/intent.py` CORE_KEYWORDS `alpha`（预期差/分歧度/拐点）。若用户话术在对话页不触发，检查词表；ZCode 侧触发由本 SKILL.md 的 description 负责。
