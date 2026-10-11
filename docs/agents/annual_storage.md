# 年报/财报存储与双路召回设计

> 状态：**设计定稿待实现**（2026-10-11 与用户对齐收敛；实现后补验证记录）。
> 上游数据链路见 [AGENTS.md](../../AGENTS.md) §3 与
> [profile_extraction_handoff.md](profile_extraction_handoff.md)；
> 研报侧既有向量库先例见 `skills/report/collection.py`（report_chunks）。

## 一、系统设计

**双层存储：PG 是真相源，Milvus 是可重建的语义索引。** 解析产物（cninfo/parse 的
md + items.json、evidence shard 的原子事实）继续以文件形态存在并由文件驱动——
PG 与 Milvus 都是从文件派生的视图，任何一边都可由 ingest 重灌。

### 1. 锚点体系（先于表结构的地基）

PG 行与 Milvus chunk 共享同一套 ID，双路结果靠锚点对齐：

```
section_id = <ts_code>:<year>:<category>:<section_key>:<sub_idx>
             例: 600519.SH:2025:ndbg:mdna:3
fact eid   = A:<year>.<sec>.<sub>.<seq>        (沿用 research EvidenceShard 契约,
                                               ts_code 作列构成全局定位)
chunk      = section_id + ':part<N>'           (仅超长小节切分; 切片永远知道母亲)
```

语义路召回 chunk 可顺锚点回 PG 取完整小节/原子事实/表格；结构路查到 fact
可顺 section_id 取上下文原文。两路互为深链。

### 2. PG 层（fin schema，两张表）

```sql
CREATE TABLE fin.annual_section (          -- 小节层: 解析产物平移 (含 items)
    id VARCHAR(64) PRIMARY KEY,            -- 即 section_id 锚点
    ts_code VARCHAR(16) NOT NULL,
    report_year SMALLINT NOT NULL,
    category VARCHAR(8) NOT NULL,          -- ndbg/bndbg/yjdbg/sjdbg
    section_key VARCHAR(16) NOT NULL,      -- mdna/governance/.../kpi3y/nonrecurring
    sub_idx SMALLINT NOT NULL,
    title VARCHAR(256), text TEXT,
    tables JSONB,                          -- 原始表格 [{name,unit,header,rows}]
    content_md5 VARCHAR(32),               -- 双库一致性对账指纹
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX ON fin.annual_section (ts_code, report_year, category, section_key);

CREATE TABLE fin.annual_fact (             -- 原子事实: evidence shard 平移
    eid VARCHAR(64) NOT NULL,
    ts_code VARCHAR(16) NOT NULL,
    report_year SMALLINT NOT NULL,
    fact_cat VARCHAR(12) NOT NULL,         -- operating/segment/statement/risk
    section_id VARCHAR(64),                -- 锚点回溯
    content TEXT NOT NULL,
    value NUMERIC, unit VARCHAR(32), period VARCHAR(16),
    quote TEXT, confidence REAL, formula VARCHAR(256),
    PRIMARY KEY (ts_code, eid)
);
CREATE INDEX ON fin.annual_fact (ts_code, report_year, fact_cat);
```

items（kpi3y/nonrecurring）本质是小节，入 annual_section，不设第三张表。

### 3. Milvus 层（annual_chunks）

**拓扑（2026-10-11 拍板）：collection 语义边界 = 文档类型**——
研报 `report_chunks`（已有）/ 年报 `annual_chunks`（新建），即"年报研报分路"。
**partition 只在 category 这条稳定轴上分**：建库即分 ndbg/bndbg/yjdbg/sjdbg 四个
（数量恒定、年报问答九成打 ndbg，一次分好免得数据进 `_default` 后再迁）。

```
fields: chunk_id(PK) | section_id | ts_code | report_year(Int64) | category
        | section_key | title | text | part(Int32) | vector
index:  HNSW + COSINE（项目既有约定）
embed:  title + text；表格不进向量（线性化噪声大），留 PG JSONB 按锚点取
```

### 4. 写路（单一 ingest 入口）

漂移都来自双写口子，规矩三条：

1. 文件是真相；cninfo 模块继续不知道任何库的存在（存储实现全在 agent 侧，
   按"业务 schema 归业务模块"惯例落 `skills/annual/`：schema.py + ingest CLI）。
2. 一个入口：读文件 → PG 按 PK upsert → Milvus 按 (ts_code,year,category)
   delete-by-expr 后插入；两边都写 content_md5。重跑幂等，重解析自然覆盖。
3. 水位对账：仿 `fin.sync_meta` 先例记 (ts_code, year, category) 的 ingest
   状态；查询侧发现 md5 不一致即知某路过期。

### 5. 读路（两个只读工具，LLM 选路 + 并行双发）

不在代码里写融合管线——"意图=LLM 选工具域"已定调，agent loop 一轮内并行
多工具已实证；融合发生在 LLM 综合阶段，锚点 ID 是对齐把手：

| 工具 | 对象 | 被选中的问题形态 | 返回形态 |
|------|------|----------------|---------|
| Text-to-SQL（annual 两表进 fin_query 词表/schema_info） | annual_fact / annual_section | 数字/枚举/过滤型（"茅台 2025 产量""哪些公司风险提汇率"） | 行集，带 eid/section_id |
| `search_annual`（向量） | annual_chunks | 表述/叙述型（"年报怎么讲产能扩张"） | top-k chunk，带 section_id + title |

混合型问题 LLM 两路并行发；两路返回都带锚点 + quote，同一 section_id 从两路
回来视为互相印证而非重复。引用规范 `cite_hint`：优先引 fact（quote 可回溯、
confidence 可审计），chunk 只作定位。

## 二、需求优化（查询模式与量级驱动）

- 事实查询是**过滤不是相似**：evidence 原子（value/unit/period/quote）是关系
  数据，"查茅台 2025 经营数据事实"是精确过滤；向量检索对数值查询无优势且打散
  quote↔数字对应。审计回溯（quote/confidence/公式）要求精确性。
- 语义查询服务 agent 问答召回：无精确键的表述类问题（"哪家公司自认汇率风险"）。
- 量级：全市场 5 年 AR+H1 ≈ 5.4 万份 × 每份几十小节块 ≈ 两三百万向量——
  对 Milvus 是小库，单 collection 无压力；PG 千万行级事实同样轻松。
- Q1/Q3 加入强化结构优先：季报短、表格密、几乎无叙述章节，价值全在结构化数字。

## 三、问题（设计过程中否决的方案）

| 否决方案 | 否决理由 |
|----------|----------|
| 解析产物直接灌 Milvus 当主存储 | 向量库丢结构——解析链的全部价值就是把财报变结构；quote 回溯/审计要精确性 |
| collection=申万行业、partition=个股 | 身份轴是 (股,年,类别) 不是行业；跨行业检索 scatter 31 个 collection 成最贵路径；行业是易变维度（重分类/IPO/退市=跨库迁移），产业链股票横跨申万；5400 partition 触管理上限；标量索引同样能做点查裁剪 |
| 代码级 cross-encoder rerank / 双路合并管线 | 无评测数据前优化是盲的；LLM 融合 + 锚点对齐已够，四层评测建起来再补 |
| PG 中文全文检索（zhparser/pg_bigm） | 把语义路的活干坏；关键词 ILIKE 兜底，语义归 Milvus，各干各的 |
| 表格线性化进向量 | 表格向量是噪声；留 PG JSONB 按锚点取 |

## 四、处理方案（待实现落地顺序）

1. `skills/annual/schema.py` + ingest CLI（文件→PG）；research EvidenceShard
   读取改走 PG（契约字段即列，load_shard 平移）。
2. annual 两表进 fin_query 词表/schema_info，agent 数字题先跑通。
3. embedding 模型选型（**当前唯一硬阻塞**）→ 建 annual_chunks（四 category
   partition）→ ingest 补向量写入。
4. `search_annual` 工具 + 沙盒词表，双路召回进评测集（数字题必须命中 fact 路
   且值正确；表述题必须命中 section）。

**待定项（后续构思）**：文档块切分策略（小节原样 / 滑窗 / 表格独立成块——
锚点字段已解耦切分迭代与存储拓扑）；Q1/Q3 解析规则（短报告无三~七节结构）。

## 五、验证记录

设计阶段（2026-10-11），尚未实现——上链各组件现状：
cninfo 获取/解析/证据内核已解耦就绪（tools/cninfo，零项目依赖）；evidence
shard JSON 产物格式即 annual_fact 列定义来源；Milvus report_chunks 先例验证
HNSW+COSINE + 业务侧 collection 定义模式可行。实现后在此补：ingest 幂等重跑
验证、双路召回评测、md5 对账用例。
