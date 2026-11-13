# 模块边界与渐进重构方案

> 2026-11-12；状态：首轮边界收敛已落地；剩余存储访问债务逐项迁移。

## 一、系统设计

保留模块化单体及现有 `api → harness/skills → tools → storage` 契约，core 提供通用设施。
优化目标是让职责与依赖清晰，不以文件数量减少为目标，也不为个人平台引入微服务、消息中间件或额外数据库。

## 二、需求优化

新增指数成分股池、5/15/30/60 分钟行情、新闻快照前，先稳定数据访问边界。
领域编排继续归 skills；采集调度归 jobs；外部数据接口归 tools；所有 SQL 与连接归 storage。
MCP 与 HTTP 只做协议、校验、鉴权和结果转换，不能成为另一套查询实现。

## 三、问题

重构前静态统计（不含 tests）：`storage/pg_schema.py` 1322 行，`skills/report/skill.py` 765 行，
`harness/loop.py` 630 行，`harness/mcp/tools/financial.py` 582 行。
仅 harness/skills 中就有 16 个模块直接导入 storage；代码实态尚未满足声明的分层契约。

| 位置 | 具体问题 | 优先级 |
|---|---|---|
| API、Skill、MCP 工具中的 SQL | 同一数据存在多个读取入口，协议与数据访问混合 | P1 |
| `tools/market/sync_index.py` | DDL、供应商调用和业务 upsert 共处一个事务；不适合新增分钟任务照搬 | P1 |
| `storage/pg_schema.py` | 行情/财务/研报/视图/ensure 混合，新增域继续堆积 | P1 |
| `harness/loop.py` | 快车道、提示词、工具执行和循环混合，测试替换范围过大 | P2 |
| `api/finance.py` | 对话、数据字典、产业链生成/保存共用模块 | P2 |
| `web/src/` | 页面、请求、图表和样式平铺；index.css 862 行 | P2 |
| MCP 两套 server | 传输实现不同但共用 REGISTRY；不能因名称相近就强行合并 | P2 |
| 项目根pip/pytest临时目录 | 默认临时目录落在根目录，安装/测试结束后残留混入工作区 | P2 |

## 四、处理方案

建议先按以下边界拆分；旧导入路径与 CLI 入口保留兼容门面，迁移调用方后再移除：

| 当前模块 | 目标职责 |
|---|---|
| `storage/pg_schema.py` | `storage/schema/` 按 market/financial/reports/auth/views 分组；旧模块重导出兼容符号 |
| 分散的查询 SQL | `storage/repositories/` 按市场、财务、研报、新闻提供仓储；优先迁移重复/高频查询 |
| `harness/loop.py` | 保留公共入口；提取 fast_lane、execution、prompts，保持 run_agent_stream 协议 |
| `skills/report/skill.py` | skill 负责领域入口，pipeline 编排抽取/OCR/入库续跑，retrieval 组织检索 |
| `api/finance.py` | chat/chains/schema 三个路由模块；聚合出口保持 URL 与鉴权不变 |
| `web/src/` | `features/chat`、`features/reports`、`features/market`、`features/news`，共用组件进 components，API/协议进 lib |

适合合并的是重复的代码职责：SSE 格式化、供应商限流重试、分页/水位线、数据库 upsert、向量集合生命周期。
通用代码只在至少两个真实调用点出现相同行为时提取，避免把领域差异塞进万能 utils。
调度器可共享时间/重试工具，但行情、研报与新闻管线分别保留；FastMCP 与 legacy server 保留协议差异，只共享工具定义和执行结果契约。
`skills/report/collection.py` 与 `skills/content/collection.py` 的存储行为迁往 storage，集合字段定义仍按领域独立。

顺序：仓储与 DDL 边界 → 研报/行情采集编排 → Agent 执行拆分 → 前端按功能迁移。
新增行情/新闻接入在新边界中实现，不等待全仓重构完成；方案见 [数据源选型](data_sources.md)。

2026-11-13工作区整理：核验21个`pip-*`目录均为旧安装的空目录、元数据或wheel，
运行依赖从虚拟环境/已配置site-packages加载，sys.path不含这些残留。
删除操作被自动审批策略拦截，改用可恢复归档：
`output/cleanup-archive/20261113-pip/`保存21目录与移动清单，
`output/cleanup-archive/20261113-tests/`保存已结束测试的345个临时文件。
未将有用途的维护脚本或用户文档当作缓存处理。根目录pip/pytest残留已移出；不宣称已永久删除或释放磁盘。
`.gitignore`明确排除安装/测试缓存；后续命令用`output/tmp`，同时设置进程TMPDIR/TEMP/TMP并确认生效，
不改用户系统环境设置。

## 五、验证记录

2026-11-12 首轮实施：

- `storage/schema/` 按 stock/financial/views/aliases/reports/industry/auth/market 分域；DDL连接单独归档。原 `pg_schema` 的53个公开常量值逐一验证不变，旧导入路径保留。
- `storage/repositories/` 收敛市场、认证和财务查询持久化；FinQuery Skill 与MCP财务查询共用 `tools/finance/query.py` 的校验/只读执行路径。
- `api/finance.py` 改为兼容聚合；agent/chains/schema独立，SSE编码收敛到 `api/events.py`，新增market查询入口只读PG。
- `harness/prompts.py` 与 `harness/execution.py` 从循环中抽出，原函数名和覆盖测试接缝保留。
- 新60F入库以 tools供应商 → jobs编排 → storage仓储运行，无DDL/网络混在业务事务中。
- `jobs/check_boundaries.py` + 自动测试阻止新增跨层导入；现存50个历史导入逐项登记（具体边及次数以JSON为准），不是已消除全部债务。前端按功能目录迁移及其余研报仓储迁移仍待实施。
- 新增 `query_intraday` 只读工具，全部工具33个，其中30个只读；分钟请求绕过日线快车道。

无调用引用的Vite模板文件和原有未跟踪目录未删除；不把他人临时文件混入本轮提交。
完整数据入库验收见 [数据源记录](data_sources.md)。

验收：382项Python测试通过，分层检查无新增违规；真实MCP stdio握手、30只读工具清单和BJ分钟查询通过。
独立MCP启动显式初始化配置，不再依赖行情模块导入的副作用。

2026-11-12股票看板：新增 `web/src/market/` 功能目录，局部图表/样式与页面拆分；
`skills/trend` 只负责纯策略与领域组合，`tools/market/trend_data` 隔离仓储，
`jobs.scan_trend` 离线发布快照，HTTP只读取。新增DDL归 `storage/schema/trend.py`。
分层检查仍无新增违规，既有50项依赖债务未扩大。详情见 [股票看板](../frontend/stock_dashboard.md)。

2026-11-13：根目录`pip-*`目录0个、旧pytest临时目录已移出；依赖加载与API健康正常。
K线图表实例/请求缓存/纯数据更新判别各司其职；8项Node校验及行情API/边界8项Python测试通过，
前端构建通过，无新增分层违规。实屏交互验收见[股票行情记录](../frontend/stock_dashboard.md)。
