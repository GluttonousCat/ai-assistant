# ai-assistant

智能投研助手 —— 知识星球爬虫 + Tushare 数据分析 + Agent ChatBI。

## 功能

- **知识星球爬虫**: 话题/评论/文件采集（反检测、增量爬取）
- **Tushare 行情**: A股日线数据抓取入库（MySQL）
- **K线技术分析**: ADX / POC / Wyckoff 信号
- **Agent 对话**: LangGraph 驱动，意图识别 -> 路由 -> 工具执行
- **四大 Skill**（开发中）: ChatBI 数据问答 / 公司基本面分析 / 研报解读 / K线技术分析

## 项目结构

```
ai-assistant/
├── app.py              # FastAPI 入口
├── config.yaml         # 非敏感配置
├── .env                # 密钥（不纳入版本控制, 参考 .env.example）
│
├── agent/              # Agent 层（大脑）: state / graph / nodes
├── skills/             # Skill 层（领域方法论）: chatbi / company / report / kline
├── tools/              # Tool 层（原子能力）: zsxq 爬虫 / market 行情 / kline 指标
├── llm/                # LLM 客户端封装
├── storage/            # 数据层: sqlite(业务) + mysql(行情)
├── api/                # API 层: router + schemas + ws
├── core/               # 基础设施: config / logger / lifespan
├── utils/              # 通用工具: paths / helpers
└── cli/                # 交互式命令行
```

分层调用关系: `api -> agent -> skills -> tools -> storage`

## 快速开始

### 1. 安装依赖

```bash
pip install -e .
# 或开发模式
pip install -e ".[dev]"
```

### 2. 配置密钥

```bash
cp .env.example .env
# 编辑 .env, 填入 OPENAI_API_KEY / TUSHARE_TOKEN / MYSQL_PASSWORD / ZSXQ_COOKIE
```

### 3. 启动服务

```bash
uvicorn app:app --reload --port 8208
```

### 4. 交互式 CLI（知识星球爬虫）

```bash
python -m cli.interactive
```

### 5. Tushare → PostgreSQL 全量日K回填

```bash
# 全量回填 daily / adj_factor / daily_basic (2000-至今)
python -m tools.market.sync_tushare

# 指定区间 / 指定表 (支持断点续传, 可重复执行)
python -m tools.market.sync_tushare --start 20200101 --end 20231231
python -m tools.market.sync_tushare --tables daily,adj_factor
```

要求: Tushare token 积分 >= 2000 (daily 200次/分钟, adj_factor/daily_basic 200次/分钟)

### 6. Tushare → PostgreSQL 财务数据回填 (fin schema)

```bash
# 全量回填 income / balancesheet / cashflow / fina_indicator (全部股票)
python -m tools.market.sync_financial

# 仅回填利润表 / 限制前100只测试
python -m tools.market.sync_financial --tables income
python -m tools.market.sync_financial --limit 100
```

财务接口 (income/balancesheet/cashflow/fina_indicator) 需 2000 积分, 按股票逐只获取。
数据按报告期入库 (report_type=1 合并报表), 支持断点续传 (按 ts_code)。

### 6. PG 行情数据仓库 (stock schema)

| 表 | 说明 | 数据源 |
|----|------|--------|
| stock_basic   | 股票池 (主板/创业板/科创板, 剔除北交/B股/ST) | stock_basic |
| trade_calendar | 交易日历 | trade_cal |
| daily         | 日K行情 (OHLCV + 涨跌幅) | daily |
| adj_factor    | 复权因子 | adj_factor |
| daily_basic   | 每日估值 (PE/PB/换手/市值) | daily_basic |
| sync_meta     | 同步水位线 (断点续传) | - |

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | /crawl/latest | 爬取最新话题 |
| POST | /crawl/historical | 爬取历史数据 |
| POST | /crawl/incremental | 增量爬取 |
| POST | /files/collect | 收集文件列表 |
| POST | /files/download | 下载文件（每次1个） |
| GET  | /stats/{group_id} | 数据统计 |
| POST/GET | /accounts | 账号管理 |
| WS   | /ws/{session_id} | Agent 实时对话 |
| POST | /agent/chat | Agent 对话（阶段二接入） |

## 开发路线

- [x] **阶段一**: 结构重构（迁移/精简/修bug/密钥外置）
- [ ] **阶段二**: Agent 骨架（LLM 意图识别 + Skill 注册 + 主图路由）
- [ ] **阶段三**: ChatBI（Text-to-SQL 数据问答）
- [ ] **阶段四**: K线分析（日K已有, 接入分钟K）
- [ ] **阶段五**: Company / Report 分析

## 知识星球使用说明

1. 浏览器登录 wx.zsxq.com, 复制 Cookie
2. 写入 `.env` 的 `ZSXQ_COOKIE`, 或通过 `/accounts` 接口添加
3. 群组 ID 在星球页面 URL 中获取
4. 爬取频率已内置反检测延迟, 请勿调低
