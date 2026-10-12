# 实时监控页 (Monitor)

> 侧边栏「实时监控」（adminOnly），SSE 每 2 秒推一帧。更新：2026-10-12

## 系统设计

```
harness/loop.py ──┐ 会话起止/漏斗路径/轮次/工具事件
harness/llm.py  ──┤ LLM 调用(用途/耗时/成败/tokens)      core/metrics.py ──> api/monitor_api.py ──SSE──> web/src/Monitor.jsx
                  ┘                                      (内存注册表+锁)      (/state 快照 /stream 推流)    (平台侧边栏页)
```

- **core/metrics.py**：纯内存注册表（计数器/工具与 LLM 分维/活跃会话/事件环形 150 条），
  `record()` 全吞异常——监控绝不影响主链路；按 local date 键控跨天翻零；重启清零
  （页面口径 =「自服务启动以来」，同时展示 uptime）
- **api/monitor_api.py**：`POST /api/monitor/stream`（SSE 2s/帧）、`GET /state`（首屏/调试），
  整组 `require_admin`；帧内附带 PG 侧状态（延迟/研报管线计数/同步水位线新鲜度，
  10s 缓存避免每帧查库）
- **Monitor.jsx**：状态灯（PG/研报管线/行情/LLM 网关）+ 指标卡（会话/进行中/工具失败率/
  tokens/域升级/译文数）+ 三级漏斗分布条 + 活跃会话表 + 工具排行 + LLM 用途表 + 事件流 +
  数据新鲜度；SSE 复用 `platformApi.sseStream`（POST + JWT 头），可暂停/恢复

## 需求优化

- 快车道/域子Agent/全工具循环的**漏斗命中率**直接可观测（设计是否生效的回答）
- LLM tokens 按 purpose 汇总 = 成本监控的第一手数（思考模式烧 token 有实数了）
- 事件流环形缓冲 150 条，够回看一次故障窗口；全量归因仍走 logs/trajectories/

## 问题

- 进程内指标重启即清零，无历史曲线（当日口径）
- Milvus / 爬虫 quota 未进状态灯（暂无廉价探针）

## 处理方案

- 历史曲线：夜间聚合任务扫 trajectory JSONL 落 PG（规划中，P0 消化已有数据后再上）
- 重启即清零属可接受设计：页面明示「自服务启动」口径

## 验证记录

- 2026-10-12：metrics 冒烟（会话/轮次/工具/LLM 计数与快照一致）；`_build_state()` 真调
  PG ok/latency/pipeline/freshness 正确；harness 27 测试全过；`npm run build` 通过
