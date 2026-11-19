# 数据库命名与兼容迁移约定

> 2026-11-19；已部署首轮行情查询索引，保留现有物理表名。

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

行情截止索引由 `storage.schema.market.deploy_cutoff_indexes()` 显式部署。先确保行情及回测输入表存在，再从项目根执行：

```powershell
.venv/Scripts/python.exe -c "from core.config import get_config; get_config(); from storage.schema.market import deploy_cutoff_indexes; deploy_cutoff_indexes()"
```

部署使用独立连接和 `CREATE INDEX CONCURRENTLY`，应用启动不会建这些索引。若部署中断留下无效索引，
先在独立自动提交连接上核对该索引的状态，再用 `DROP INDEX CONCURRENTLY` 清理无效索引并重跑。
回滚时可用同一方式移除本轮新增的 `stock.idx_intraday_bar_60f_native_end`、
`stock.idx_adj_factor_trade_date`、`stock.idx_daily_price_limit_trade_date`。

数据字典为 Agent 和 MCP 展示 `schema.table` 与实际列名。股票日K、每日估值与换手、股票主数据、
指数日K、分钟K线分开描述。其中 `stock.index_daily.ts_code` 表示指数代码；
`stock.intraday_bar` 仍按周期、来源和复权方式区分。

## 五、验证记录

2026-11-19 在 PostgreSQL 核对 35 张非研报实体表，均含数据。旧量化两张跟踪表共 33,234 行，保留原历史。
三个行情截止索引均已部署并验证有效。相同单次实测中，60F、复权因子、涨跌停价的最新记录查询均改为
反向索引扫描，数据库执行时间分别约 0.10、0.07、0.09 毫秒；新增索引合计约 141 MiB。
15:00 前后日K截止的交易日切换与旧口径一致。研报相关表和解析未参与迁移。
以真实 PG 字典逐一核对 MCP 返回的 15 个表及视图和所列字段。stdio 和 FastMCP 均通过，
注册 33 个工具，只读清单 30 个。全量离线回归 875 项通过。
