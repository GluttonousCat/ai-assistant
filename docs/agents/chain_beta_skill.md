# 产业链 Beta / 个股 Alpha Skill 设计

> 模块：`beta_alpha/`（自治子系统：产业链挖掘 + 个股预期差）；Agent 会话入口 `.agents/skills/beta-skill`、`.agents/skills/alpha-skill`
> 更新：2026-09-03

## 一、系统设计

### 1.1 目标

- **Beta Skill**：把"趋势倒推产业链"（AI→服务器→GPU/HBM/光通信/PCB→上游 CCL/树脂/铜箔/电子布）变成可查询、可验证的结构化分析：链条解析 → 环节→标的映射 → 环节行情/景气验证 → LLM 中文综述。
- **Alpha Skill**：围绕"预期差"框架做个股分析：券商盈利预测分歧度 × 财务拐点 × 估值分位 × 研报观点四象限。
- 数据基座：Tushare PG 数仓（含 `fin.fina_mainbz` 主营构成）+ 知识星球研报库（tags/盈利预测/链抽取）。

### 1.2 三层结构（Beta）

| 层 | 职责 | 数据来源 |
|----|------|----------|
| 知识层 | 产业链模板（环节树+关键词） | `beta_alpha/chains/*.yaml`（LLM 领域知识固化，git 版本化）+ `fin.chain_extract`（研报动态增量，Phase 2） |
| 映射层 | 环节→标的三源打分 | ① 主营构成收入占比（硬证据）② 申万 L2 行业（兜底）③ 研报 tags/链抽取（佐证） |
| 量化层 | 环节指数/超额收益/景气度 | `stock.daily`(前复权) 流通市值加权环节组合 vs 沪深300；`fin.fina_indicator` 营收增速中位数；研报热度计数 |

### 1.3 映射分档规则

- **强**：主营构成中环节关键词命中业务的收入占比 ≥30%（年报/最新报告期口径，取 `fin.v_main_biz` 最新期，剔除维度表头/合计/其中子项）
- **中**：占比 10~30%；或无主营证据但申万 L2 命中且近 1 年研报提及 ≥2 篇
- **弱**：仅行业归属命中（证据不足，不入环节指数）
- 环节指数成分 = 强+中（按占比降序截取前 20），流通市值加权

### 1.4 代码形态（分层契约遵守）

```
beta_alpha/                      自治模块（对标 range_trading/, 对外只暴露稳定入口）
├── chains/*.yaml                种子链模板（专业知识载体）
├── analysis/chain_analysis.py   原子能力：load_chains/match_chain/map_chain（纯 SQL+pandas）
├── analysis/chain_extract.py    原子能力：研报正文→环节抽取入库（Phase 2）
├── skills/beta.py               编排：链解析→映射→指标→LLM综述
├── skills/alpha.py              预期差四象限（Phase 3; 纯函数 to_yuan/classify_drift 可测）
├── prompts.py                   全部提示词（链综述/链抽取/四象限综述）
├── streaming.py                 SSE 编排 stream_chain/stream_alpha（api/finance.py 薄包装消费）
├── schema.py                    fin.chain_extract DDL（模块内 ensure）
└── tests/                       pytest 11 例（模板结构/泛词黑名单/分档/单位归一/漂移判定）
.agents/skills/beta-skill/       Agent 会话模块（SKILL.md 驱动 ZCode 等直接调用上述 CLI）
.agents/skills/alpha-skill/      同上；references 存维护手册, scripts 存排查工具
```

意图路由：`agent/intent.py` 新增 `chain`（关键词：产业链/链条/环节/上游/下游）与 `alpha`（预期差/分歧/拐点）；`agent/fin_graph.py` 路由到新节点；`api/finance.py` SSE 增加对应分支（stage: intent→chain/alpha→map/compute→data→delta→done）。

## 二、需求优化

1. **种子模板 vs 现场推理**：每次让 LLM 现推产业链会漂移且无法做量化验证 → 固化为 YAML 模板（可手编、可版本化），LLM 只负责"用户话术→链/环节"的解析与最终综述。
2. **主营构成先于概念板块**：Tushare 概念板块归属噪声大（蹭热点），主营构成是财报口径的硬数据 → 强档只认主营占比。
3. **研报数据是差异化资产**：环节研报热度、`fin.report_forecast` 盈利预测分歧度是平台独有数据，分别作为环节热度指标与 alpha 预期差核心。
4. **环节指数不建新表**：由成分股日线实时计算（120 交易日窗口），避免维护物化指数的复杂度。

## 三、问题（设计期识别）

| 问题 | 决策 |
|------|------|
| fina_mainbz 接口无 ann_date/update_flag，但一次调用返回全部历史×全部维度（bz_code 自带 P/D/I 标签） | 每股 1 次调用；维度标签存 biz_type；`455006000` 等编码=销售模式等补充维度，保留 |
| 主营构成脏数据：维度表头行（产品/行业/地区，值=总收入）、合计行、冒号分层子项重复计入 | 视图 `fin.v_main_biz` 统一清洗 + `is_sub_item` 标记，占比分母剔除 |
| 环节关键词与 SW L2 行业名不对齐（如"光模块"≠"通信设备"） | 节点 YAML 显式声明 `sw_l2` 列表，不做模糊匹配 |
| 研报 tags 的 ts_code 覆盖不全 | 股票级研报证据只作加分项；环节级热度用全库 tag 命中计数 |
| SSE 异步上下文里的同步重 IO | 环节计算包 `asyncio.to_thread`（AGENTS 已知坑） |
| 深度提取只喂正文前 1.2 万字，链抽取同受限制 | 链抽取沿用 1.2 万字窗口（Phase 2 接受；全文分段抽取留待后续） |

## 四、处理方案（分期）

- **Phase 1（存量数据）**：种子链 YAML（AI算力/半导体国产化/人形机器人）+ 三源映射打分 + 环节指数/超额收益 + beta skill 上线对话页（SSE 分支 + 徽章）。
- **Phase 2（研报增量）**：`fin.chain_extract` DDL + 深度提取后挂后台链抽取（fail-safe 不阻塞主链路）+ 映射融合研报环节热度 + AgentChat 升级（markdown 表格、步骤卡片、新徽章）。
- **Phase 3（alpha）**：`beta_alpha/skills/alpha.py` 预期差四象限上线（不依赖年报全文）；cninfo 年报定向爬取复用 PDF→OCR→LLM 管线（后续独立迭代，见 §6）。
- **结构重构（2026-09-03 晚）**：三 Phase 落地的散落代码（skills/beta、skills/alpha、tools/finance/chain_*、pg_schema 的 chain_extract DDL、api 的 SSE 编排）整体迁移为自治模块 `beta_alpha/`，api 只留薄包装，旧位置删除，新增 11 例单元测试。
- **产品化（2026-09-04）**：新增「产业链」独立页面 `web/src/ChainView.jsx`（链 chips → 环节卡片墙 → 按需 AI 流式解读；标的 chip 跨页跳对话页自动发起预期差提问，`chat:pending` 机制）；后端只读端点 `GET /api/v1/chains`、`GET /api/v1/chains/{id}/analysis`（无 LLM）；Markdownish 抽为共享渲染器。
- **种子链锻造（2026-09-05）**：`beta_alpha/forge.py` 双通道建链——LLM 生成闭环（草稿喂真实披露语料样本 → 结构校验 → 关键词命中率 vs 全量主营构成 → 零命中自动修正一轮，超时重试）+ 用户自建（`_template.yaml` 模板 / 网页弹窗编辑，`save_chain_yaml` 统一校验落盘并清缓存）；`--check` 命令出每环节每关键词命中报告；API `POST /api/v1/chains/forge/stream`（SSE）与 `POST /api/v1/chains/save`；前端「＋ 新建链」弹窗（生成/手写共用文本框，命中率 chips 红绿标注）。实测：苹果代工链（apple_oem_chain，人工修正 2 个零命中环节后 EMS 23强/PCB 16强）、磷化工链（phosphorus_chemical_chain 生成即 2 轮收敛）。

## 五、验证记录

| 日期 | 验证项 | 结果 |
|------|--------|------|
| 2026-09-03 | `fina_mainbz` 同步 3 只测试股 → 450 行，占比加总 99.94%（平安银行中报 P 维度），视图零脏行 | ✅ |
| 2026-09-03 | 全量回填（断点续传，5005 只白名单，监控窗口 + logs/sync_mainbz.log） | ✅ 进行中 |
| 2026-09-03 | 意图识别：chain/alpha 核心词 0.90；既有 query/report 不回归（"查询平安银行的营收→compare" 为存量双实体误判，非本次引入） | ✅ |
| 2026-09-03 | AI算力链端到端：CCL 120日超额+121.7%/PCB+53.2%/光模块+39.3%/IDC-12.2%（与行情实态一致）；沪电"通讯市场板"应用口径命中（补关键词后 46.6% 强档） | ✅ |
| 2026-09-03 | alpha 四象限（中际旭创）：营收+182.5%/毛利率46.25%上行/PE 37.2分位 vs PB 78.6分位；预测覆盖空=研报库A股覆盖现状，如实呈现 | ✅ |
| 2026-09-03 | SSE 事件序列：chain=stage×4→data→delta×N→done；alpha=stage×3→data→delta×N→done | ✅ |
| 2026-09-03 | 链抽取回填 6 篇→64 环节（AI CCL→生益/AI PCB→沪电/PTFE 等动态环节）；单篇幂等重抽 ✓；融合 chain_mentions（PCB=3/光模块=1） | ✅ |
| 2026-09-03 | `npm run build` 通过；registry=[fin_query,report,scanned_report,beta,alpha]；sql_guard 对新表/视图放行 | ✅ |
| 2026-09-04 | 产业链页产品化：端点直连验证（3 链/12 环节/单环节下钻/404 路径）、无 JWT 401（中间件自动覆盖）、`npm run build` 通过、服务重启 health 200 | ✅ |

### 修复记录（实现中发现）

| 问题 | 根因 | 方案 |
|------|------|------|
| 环节指标全 None | PG NUMERIC→Decimal 使 pandas object dtype，除零抛异常；且 `stock.daily.adj_factor/close_adj` 预计算列为空 | 数值统一 astype(float)；价格改 `close × stock.adj_factor 表` 现算 |
| 沪电股份 PCB 漏匹配 | 公司按应用口径披露（"企业通讯市场板"）非产品口径 | yaml keywords 补应用词（通讯市场板/汽车板） |
| 弱档被 SW 行业淹没（半导体 178 只） | 仅行业归属即入成员 | 规则改为"须主营证据或研报提及" |
| 泛词误报（"整机"命中输送机整机、"树脂"命中涂料树脂） | 关键词粒度 | 删"整机"；"树脂"→电子/环氧/特种树脂 |
| alpha 全象限空 | stock_kb.match_stock 返回 (ts_code, 别名) 与假设顺序相反 | _resolve_stock 显式规范为 (名称, 代码) 并查官方名 |
| 产业链页 HTTP 524 (2026-09-04) | fina_mainbz 回填至百万行级后，逐环节查询走 v_main_biz 全表物化（12次）+ daily/daily_basic 按股票过滤触发 trade_date 主键下的全表扫描（~40次），单次分析 >100s，Cloudflare 超时 | 三层改造：①绕开视图，一次索引扫描拉全市场最新期 P 构成（10min 缓存），关键词内存匹配 ②研报 tags 一次拉取内存匹配 ③环节行情/估值/财务批量合并为 4 次查询 + 结果 300s TTL 缓存。全链分析 25.5s→9.9s（热 0s），超额数值与改造前完全一致 |

## 六、后续（不在本期）

1. **cninfo 年报定向爬取**：目标池=链环节成分股+高覆盖股，复用 `tools/zsxq/downloader` 反检测与 `report_extractor`/`pdf_vision` 提取管线；入库 `fin.annual_report_text`；喂给 alpha 定性象限（管理层指引/在手订单/送样进展——主营构成覆盖不到的"CPO 送样"类信息）。
2. 深度提取全文分段（突破 1.2 万字窗口）。
3. 产业链树前端可视化（`data.nodes` 已含 edges 结构，组件化在 Reports 弹窗成熟后复用）。
4. alpha 挂周报调度自动产出"预期差清单"。
