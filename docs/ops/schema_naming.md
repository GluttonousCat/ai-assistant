# 数据库命名与兼容迁移约定

> 2026-11-12；本轮完成设计审核与命名规则，不执行物理表重命名。

## 一、系统设计

现有 PostgreSQL：`stock` 存行情、标的与行业，`fin` 存财务和研报，`auth` 存用户。
DDL已按域收敛到 `storage/schema/`；`storage/pg_schema.py`保留兼容导出，认证DDL归入schema/auth，由core/security调用ensure。
Text-to-SQL 依赖 schema_info、SQL guard 白名单、业务视图和提示词；同步程序依赖 Tushare 字段名。

## 二、需求优化

统一新对象的语义和命名，减少同一业务含义的多个别名，同时保留上游契约。
不为格式整齐重命名 Tushare 字段；面向产品的中文标签由前端/字典提供。

## 三、问题

| 现状 | 风险 | 处理 |
|---|---|---|
| `fin` 同时容纳财务三表和研报 | 业务域边界模糊 | 未来研报域可引入 `research`，必须先建立兼容契约 |
| `balancesheet` / `cashflow` 等上游名称不符合统一分词习惯 | 直接改名破坏同步与 SQL prompt | 保留物理名，在服务/视图提供清晰语义 |
| 主键 `id` / `report_id` 混用 | JOIN 与日志难辨认 | 新领域表统一 `<entity>_id`；现有 forecast.id 不原地改名 |
| `TIMESTAMP` 与业务 DATE 混合 | 时区解释不清 | 新审计时间用 TIMESTAMPTZ，交易/报告期继续 DATE；旧数据须确认原时区才迁移 |
| DDL ensure 与业务初始化分布多处 | 无升级版本与回滚审计 | 下一阶段引入迁移版本登记，保留独立连接、锁超时和幂等性 |

## 四、处理方案

新表采用 snake_case、单数领域实体，例如未来 `research.report`、`research.report_forecast`；
领域对象用明确主键，外键保持同名；时间字段 `_at`，业务日期 `_date`，布尔 `is_`/`has_`。
金额与比率列必须在列注释中写明单位、币种和累计/单期口径；不把数值格式化存成字符串。
索引命名 `idx_<table>_<columns>`，唯一约束 `uq_<table>_<columns>`，外键 `fk_<table>_<reference>`。

旧对象迁移顺序：

1. 统计调用方、真实数据量、约束、索引、权限、视图与提示词依赖，做备份并在副本演练。
2. 在独立连接执行幂等迁移，设定锁等待上限；大量索引单独安排，不能靠启动请求执行长 DDL。
3. 提供明确列清单的兼容视图或仓储适配；写入维持唯一入口，禁止双写真相源。
4. 同步、查询、去重、外键、角色权限、SQL guard、schema_info 和 prompt 一起回归。
5. 分版本切换调用方，观察稳定后才能移除兼容层；上线前备好回滚步骤。

索引候选必须以 EXPLAIN 与调用频次为证据，不能仅根据字段名添加。
现有日线主键 `(trade_date, ts_code)` 与按股票查询索引 `(ts_code, trade_date)` 用途不同，不能机械删重。

## 五、验证记录

本轮检查 `storage/pg_schema.py` 的行情、财务、研报 DDL、现有索引及 schema_info/SQL guard 调用契约。
未连接生产库进行 DDL、数据清洗或 EXPLAIN；这些步骤属于后续迁移的必要验收条件。
