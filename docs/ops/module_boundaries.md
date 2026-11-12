# 模块边界与渐进重构方案

> 2026-11-12；状态：已审核并形成方案，以下目录迁移尚未执行。

## 一、系统设计

保留模块化单体及现有 `api → harness/skills → tools → storage` 契约，core 提供通用设施。
优化目标是让职责与依赖清晰，不以文件数量减少为目标，也不为个人平台引入微服务、消息中间件或额外数据库。

## 二、需求优化

新增指数成分股池、5/15/30/60 分钟行情、新闻快照前，先稳定数据访问边界。
领域编排继续归 skills；采集调度归 jobs；外部数据接口归 tools；所有 SQL 与连接归 storage。
MCP 与 HTTP 只做协议、校验、鉴权和结果转换，不能成为另一套查询实现。

## 三、问题

本轮静态统计（不含 tests）：`storage/pg_schema.py` 1322 行，`skills/report/skill.py` 765 行，
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

## 五、验证记录

已完成模块行数、AST 导入统计和关键调用方阅读，未移动运行时代码。
迁移实施时需保持既有 367 项 Python 测试、前端协议测试、lint/build，并增加导入兼容、CLI/MCP 冒烟和数据幂等回归。
无调用引用的 Vite 模板 CSS/SVG 等可在单独清理提交中删除；本轮未删除。
