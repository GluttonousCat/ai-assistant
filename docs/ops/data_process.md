# ai-assistant 数据处理全流程 (Data Process)

> 数据层完整技术文档: 数据源接入 / 数据仓库架构 / 回填流程 / 行业分类 / 评测体系
> 更新时间: 2026-08-16

---

## 目录

- [1. 数据源概览](#1-数据源概览)
- [2. 数据仓库架构 (PostgreSQL)](#2-数据仓库架构-postgresql)
- [3. 表结构设计](#3-表结构设计)
- [4. 数据回填流程](#4-数据回填流程)
- [5. 申万行业分类](#5-申万行业分类)
- [6. 宽表视图](#6-宽表视图)
- [7. 评测体系](#7-评测体系)
- [8. 运维与常见问题](#8-运维与常见问题)
- [9. 数据使用指引](#9-数据使用指引)

---

## 1. 数据源概览

| 数据源 | 用途 | 权限 | 调用限制 |
|--------|------|------|---------|
| Tushare Pro | A股行情 + 财务数据 | 2000 积分 | 200次/分钟 (各接口) |
| 知识星球 | 话题/评论/文件 (研报素材) | Cookie | 反检测延迟 |

### Tushare 积分权限
- 2000 积分: daily 200次/分钟, adj_factor 200次/分钟, daily_basic 200次/分钟
- income/balancesheet/cashflow/fina_indicator: 需 2000 积分, 按股票获取
- income_vip (全市场按季度): 需 5000 积分

### 边界约束
- 远程 PG 仅通过 SQL (psycopg2) 访问, 不做任何服务器级操作
- 密钥外置 `.env` (OPENAI_API_KEY / TUSHARE_TOKEN / PG_* / ZSXQ_*)

---

## 2. 数据仓库架构 (PostgreSQL)

### 连接信息
- 远程 PG: `[PG-HOST:REDACTED]`, database=`data`, user=`postgres`
- 连接池: `storage/pg.py` PgPool (单例)
  - `search_path=stock,fin,public` (LLM 生成的无 schema 前缀表名可解析)
  - DataFrame 批量 UPSERT (`ON CONFLICT` 幂等)

### Schema 划分
```
stock (行情)
├── stock_basic        股票池 + 申万行业冗余列
├── stock_alias        股票别名词典
├── trade_calendar     交易日历
├── daily              日K行情
├── adj_factor         复权因子
├── daily_basic        每日估值
├── index_classify     申万行业分类 (L1/L2/L3)
├── stock_industry     申万行业成分映射 (含历史)
├── v_industry_current 行业视图 (L1/L2 双级)
├── v_daily_valuation  行情+估值宽表视图
└── sync_meta          同步水位线

fin (财务)
├── income             利润表 (95 字段)
├── balancesheet       资产负债表 (52 字段)
├── cashflow           现金流量表
├── fina_indicator     财务指标
├── v_financial_summary 财务宽表视图
└── sync_meta          按股票续传水位线
```

### 数据总量 (2026-08-16, 全量完成)

| 表 | 行数 | 覆盖范围 |
|----|------|---------|
| stock.daily | 10,592,038 | 1990-12-19 ~ 2026-08-14 |
| stock.adj_factor | 15,241,072 | 同上 |
| stock.daily_basic | 15,349,643 | 同上 |
| fin.income | 273,485 (4,738股) | 1990-12-31 ~ 2026-06-30 |
| fin.balancesheet | 247,003 (4,948股) | 1989-12-31 ~ 2026-06-30 |
| fin.cashflow | 264,798 (5,002股) | 2001-12-31 ~ 2026-06-30* |
| fin.fina_indicator | 230,192 (5,002股) | 1990-06-30 ~ 2026-06-30 |

*现金流量表 1998 年才开始强制披露, 90s 无数据属历史合理.

---

## 3. 表结构设计

### 3.1 核心表 DDL 位置
所有建表 DDL 集中在 `storage/pg_schema.py` (幂等建表, IF NOT EXISTS)。

### 3.2 关键表设计说明

**stock_basic** (股票池, 5,005 只):
```
ts_code       VARCHAR(16) PK    -- 600519.SH
symbol        VARCHAR(8)        -- 600519
name          VARCHAR(32)       -- 贵州茅台
market        VARCHAR(8)        -- 主板/创业板/科创板
industry      VARCHAR(32)       -- 东财粗行业 (110类)
industry_l1   VARCHAR(32)       -- 申万一级 (冗余快查)
industry_l2   VARCHAR(32)       -- 申万二级 (冗余快查)
status        VARCHAR(4)        -- L/D/P
```
股票池过滤: 主板+创业板+科创板, 剔除北交所(8/4) / B股(200/900) / ST / 退市.

**stock_alias** (别名词典):
```
alias  VARCHAR(32) PK    -- 简称/外号/谐音
ts_code VARCHAR(16)      -- 标准代码
```
种子 60+ 条 (平银/招行/茅台/兆易/赵姨/生益/沪电/胜宏...), 可动态扩充.

**stock_industry** (申万成分, 含历史归因):
```
index_code  VARCHAR(12)  -- 申万二级指数 801125.SI
con_code    VARCHAR(16)  -- 成分股 ts_code
in_date     DATE         -- 纳入日期
out_date    DATE         -- 剔除日期 (NULL=当前成员)
```

---

## 4. 数据回填流程

### 4.1 行情回填 `python -m tools.market.sync_tushare`

```bash
# 全量 (2000-至今)
python -m tools.market.sync_tushare
# 指定区间 / 指定表
python -m tools.market.sync_tushare --start 19900101 --end 19991231 --tables daily
python -m tools.market.sync_tushare --tables daily,adj_factor
```

特性:
- **断点续传**: 基于表内实际 MAX(trade_date), 可反复执行, 中断恢复
- **交易日历自举**: 用 daily 接口逆推 (绕开 trade_cal 限流)
- **白名单缓存**: stock_basic 落地 PG, 不回源 Tushare
- **搜索路径**: search_path 兼容无 schema 表名

### 4.2 财务回填 `python -m tools.market.sync_financial`

```bash
python -m tools.market.sync_financial              # 全量4表
python -m tools.market.sync_financial --tables income
python -m tools.market.sync_financial --limit 100  # 前100只测试
```

特性:
- 按股票逐只拉取 (income 等接口仅单股票)
- 按 ts_code 断点续传 (fin.sync_meta)
- 逐表执行: fina_indicator → cashflow → balancesheet → income

### 4.3 90s 数据补充

```bash
python -m tools.market.sync_tushare --start 19900101 --end 19991231 --tables daily,adj_factor,daily_basic
python -m tools.market.backfill_90s_fin   # 财务90s (income/balancesheet/fina_indicator)
```

cashflow 90s 跳过 (1998前无此表).

### 4.4 回填进度查询

```sql
-- 各表行数与日期范围
SELECT COUNT(*), MIN(trade_date), MAX(trade_date) FROM stock.daily;
-- 同步水位
SELECT * FROM stock.sync_meta;
SELECT * FROM fin.sync_meta;
```

---

## 5. 申万行业分类

### 5.1 数据来源
- `index_classify(SW2021)`: 行业定义 (L1 31 / L2 134 / L3 212 = 377 条)
- `index_member(index_code)`: 成分股 (含 in_date/out_date)

### 5.2 同步流程 `python -m tools.market.sync_industry`

```python
# sync_industry.py 完整流程
sync_classify(pro)       # 拉 L1/L2/L3 行业定义 -> index_classify
sync_members(pro)        # 拉 124 个二级行业成分 -> stock_industry
backfill_stock_basic()   # 回填 stock_basic.industry_l1/l2 冗余列
```

### 5.3 查询集成

**视图** `stock.v_industry_current` (个股→L1/L2 双级):
```sql
-- 茅台当前行业
SELECT * FROM stock.v_industry_current WHERE ts_code='600519.SH';
-- 白酒Ⅱ 成分排行查询 (行业+指标)
SELECT s.name, i2.industry_l2, f.grossprofit_margin
FROM stock.v_industry_current i2
JOIN fin.fina_indicator f ON i2.ts_code = f.ts_code
WHERE i2.index_l2='801125.SI';
```

**代码层**:
- `StockKB.match_industry(文本)`: 行业名最长匹配 (含罗马后缀 "白酒Ⅱ"→白酒)
- `RuleEngine` 行业分支: "XX行业的YY指标" → `build_industry_query`

**已支持查询** (验证通过):
| 查询 | 结果 |
|------|------|
| 白酒行业的毛利率 | 茅台 89.6% 居首 |
| 银行板块的市盈率 | 招行 PE 6.43 |
| 半导体板块的市值 | 半导体估值排行 |
| 食品饮料行业的净利润 | 茅台居首 |

---

## 6. 宽表视图

为简化 Agent/LLM 查询, 提供两个宽表视图:

**fin.v_financial_summary** (财务宽表, 273k 行):
三表 + 指标 join (income LEFT JOIN balancesheet/cashflow/fina_indicator),
WHERE report_type='1'. LLM 自动优先使用.

**stock.v_daily_valuation** (行情估值宽表, 1,059万行):
daily LEFT JOIN daily_basic LEFT JOIN stock_basic (含行业).

**stock.v_industry_current** (行业视图, 5,186 条当前成员):
```sql
ts_code, index_l2, industry_l2, index_l1, industry_l1
```

---

## 7. 评测体系

### 7.1 评测框架 (evals/)

```
evals/
├── generate_cases.py    # 用例生成器 (1000条, 8类模板, seed=42 可复现)
├── run_eval.py          # 评测运行器 (rule/llm 模式)
└── results/             # 结果留存 (json/csv)
```

### 7.2 评测命令

```bash
# 规则模式 (无 LLM, 快速) - 词典+模板引擎
python -m evals.run_eval --n 1000 --mode rule
# LLM 模式 (需有效 key, 测完整链路)
python -m evals.run_eval --n 1000 --mode llm
```

### 7.3 评测指标

| 指标 | 含义 |
|------|------|
| intent_accuracy | 意图识别与预期一致率 |
| sql_generation_rate | 成功生成 SQL 率 |
| execution_success_rate | 执行无异常率 |
| data_hit_rate | 非空结果率 (受回填进度影响) |
| edge_handled_rate | 边界/危险用例处理率 |

### 7.4 规则引擎评测结果 (词典+模板, 1000条)

| 指标 | 结果 |
|------|------|
| 意图准确率 | 96.0% |
| SQL 生成率 | 98.5% |
| 执行成功率 | 98.5% |
| 平均延迟 | 191ms |

> 对比: 旧正则方案 35% → 词典+模板引擎 98.5%
> 说明: 规则引擎定位"封闭域固定句式" (指标词有限), LLM 兜底复杂表达.

---

## 8. 运维与常见问题

### 8.1 代理断连
- 现象: 回填进程 ProxyError (127.0.0.1:10808) 崩溃
- 处理: 断点续传重启即可恢复
- 建议: 生产环境用重试队列/指数退避

### 8.2 Tushare 限流
- 低积分: stock_basic / trade_cal 曾 1次/小时 (已通过落地 PG 缓存绕开)
- 2000 积分: daily 200次/分钟足够
- 处理: API_SLEEP 参数控制 (行情 1.3s, 财务 1.2s)

### 8.3 90s 边界日期 None
- 现象: `_to_date(None)` 异常 (90s 早期股票 pretrade_date 为空)
- 修复: `_to_date` 统一处理 None/NaN 返回 None

### 8.4 回填进程崩溃
- 原因: 长时间运行 + 代理断连
- 处理: 重启自动续传 (表内 MAX 或 sync_meta 水位线)

---

## 9. 数据使用指引

### 9.1 查询代码示例

```python
from storage.pg import PgClient

# 单股最新财务
with PgClient() as pg:
    df = pg.fetch_df("""
        SELECT * FROM fin.v_financial_summary
        WHERE ts_code='600519.SH' ORDER BY end_date DESC LIMIT 5""")

# 行情
with PgClient() as pg:
    df = pg.fetch_df("""
        SELECT * FROM stock.v_daily_valuation
        WHERE ts_code='300750.SZ' ORDER BY trade_date DESC LIMIT 30""")
```

### 9.2 Tushare 客户端

```python
from tools.market.tushare_client import TushareClient
client = TushareClient()  # token 从 .env 读
df = client.fetch_stock_daily('600519.SH', '20230101', '20231231')
```

### 9.3 相关脚本

| 脚本 | 作用 |
|------|------|
| `tools/market/sync_tushare.py` | 行情回填 |
| `tools/market/sync_financial.py` | 财务回填 |
| `tools/market/sync_industry.py` | 申万行业同步 |
| `tools/market/backfill_90s_fin.py` | 90s 财务补充 |
| `scripts/audit_data.py` | 数据仓库一键审核 |
| `evals/run_eval.py` | 评测 |

---

*本文档覆盖数据层全部工作. 架构/Agent 相关请见 `docs/PRD.md`.*