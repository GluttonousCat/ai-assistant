# Alpha Finance Radar 文档索引

> AI Agent 请从根目录 [AGENTS.md](../AGENTS.md) 进入（项目全貌+坑清单）。
> 本索引面向人类维护者，按模块组织；每份文档统一
> 「系统设计 → 需求优化 → 问题 → 处理方案 → 验证记录」结构。
> 最后整理：2026-08-31

## 模块树

```
AGENTS.md               <- Agent 上下文入口（项目全貌/技术栈/踩坑清单）
docs/
+-- README.md           <- 本索引
+-- frontend/           前端各模块设计
|   +-- login.md             登录注册（JWT/邀请码/校验规则/密码可见性）
|   +-- chat.md              Agent 问答（SSE 事件协议/意图路由）
|   +-- reports.md           研报中心（流水线/表结构/去重/LLM Analysis/AI分析流式弹窗）
|   +-- framework_theme.md   框架与主题（技术栈/品牌 Alpha Finance Radar/深黑暗棕）
+-- agents/             Agent 与模型体系
|   +-- llm_models.md            模型路由（deepseek 文本/qwen 视觉/用途体系/关思考坑）
|   +-- scanned_report_agent.md  扫描件研报分析（PDF->PNG->视觉OCR 三层解耦）
|   +-- chain_beta_skill.md      产业链Beta/个股Alpha Skill（种子链/三源映射/环节指数/预期差四象限）
|   +-- annual_storage.md        年报存储与双路召回设计（PG真相源+Milvus语义索引/锚点/单写路/定稿待实现）
+-- ops/                运维与数据
|   +-- domain_setup.md      域名与 Tunnel 部署/桌面入口(启动器+日志查看器)/运维手册
|   +-- security.md          安全分层/外部访问事件审计/PG 加固清单
|   +-- data_process.md      数据同步全流程（历史文档）
|   +-- spider.md            知识星球爬虫设计（历史文档）
|   +-- cninfo_spider.md     巨潮定期报告爬虫（按需拉取/幂等/orgId三级策略）
+-- PRD.md              产品需求与技术研发 v1.0（历史快照，架构演进以 AGENTS.md 为准）
```

## 按场景导航

| 我想… | 看 |
|-------|-----|
| 快速理解整个项目 | [AGENTS.md](../AGENTS.md) |
| 查看技术审核、修复与后续工作 | [ops/technical_review.md](ops/technical_review.md) |
| 数据库命名与兼容迁移 | [ops/schema_naming.md](ops/schema_naming.md) |
| 工程目录、后台工作流、旧命令兼容、回归与 SQL 性能验证 | [ops/module_boundaries.md](ops/module_boundaries.md) |
| 分钟行情、非ST扩池补齐、新闻快照和指数成分股池 | [ops/data_sources.md](ops/data_sources.md) |
| 后端每天18:00趋势选股图文邮件（无需Codex） | [ops/market_digest.md](ops/market_digest.md) |
| 查看后台采集进度、运行日志和Agent监控 | [frontend/monitor.md](frontend/monitor.md) |
| 交易日17:00更新日K与看板、盘中四轮60F调度及重启补漏 | [ops/daily_sync.md](ops/daily_sync.md) |
| 股票行情、独立盘中60F观察、5年日K、连续信号合轮/归档与T+1/3/5/10跟踪 | [frontend/stock_dashboard.md](frontend/stock_dashboard.md) |
| 策略创作工作台/可组合指标条件/自然语言规则/后台回测、主板60F策略、442/532分批及研究偏差 | [agents/quant_backtesting.md](agents/quant_backtesting.md) |
| 改登录/注册逻辑 | [frontend/login.md](frontend/login.md) |
| 改问答链路/加流式 | [frontend/chat.md](frontend/chat.md) |
| 改研报展示/提取 | [frontend/reports.md](frontend/reports.md) |
| 换 LLM 模型 | [agents/llm_models.md](agents/llm_models.md) |
| 处理扫描件 PDF | [agents/scanned_report_agent.md](agents/scanned_report_agent.md) |
| 加/改产业链Beta、个股Alpha Skill | [agents/chain_beta_skill.md](agents/chain_beta_skill.md) |
| 改年报披露项提取 (items/sections) | [agents/profile_extraction_handoff.md](agents/profile_extraction_handoff.md) |
| 实现年报存储/向量召回 | [agents/annual_storage.md](agents/annual_storage.md) |
| 企微机器人接入/桥接 | [agents/wecom_bot.md](agents/wecom_bot.md) |
| 改主题/品牌 | [frontend/framework_theme.md](frontend/framework_theme.md) |
| 域名/启动/看日志 | [ops/domain_setup.md](ops/domain_setup.md) |
| 安全与加固 | [ops/security.md](ops/security.md) |

## 维护约定

1. **一模块一文档**：新功能先归入既有模块文档；确实无归属才新建（并在本索引与 AGENTS.md 登记）
2. **五段结构**：系统设计 -> 需求优化 -> 问题 -> 处理方案 -> 验证记录
3. **问题必留痕**：每个 bug 的根因+方案写入对应模块「历史问题与处理记录」表
4. **过时不删**：被取代的内容标注「历史」移入 ops/（保留决策脉络）；与代码实态冲突以代码为准并顺手修正
5. **敏感地区名词用代号 TW / HK**；bat/配置避免中文（编码坑）
