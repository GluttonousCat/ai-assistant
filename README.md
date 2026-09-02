# Alpha Finance Radar

个人智能投研平台 —— Tushare 财务数据 × 知识星球研报 × Agent 分析。

## 功能

- **用户系统**：JWT 登录注册（邀请码门槛），三级角色 —— 超级管理员（用户管理）/ 管理员（区间看板）/ 研究员
- **Agent 问答**：自然语言 → 意图路由 → Text-to-SQL 财务查询 / 研报解读，SSE 流式打字机输出
- **研报中心**：知识星球研报自动抓取入库，LLM 提取机构/标的/行业/地区/市场 + 评级与盈利预测，
  支持扫描件 PDF（视觉 OCR）、去重合并、多市场筛选
- **区间看板**：震荡区间 + 趋势 + 动量矛三系统全市场扫描（管理员可见），含信号跟踪与回测
- **数据调度**：交易日 21:00 Tushare 行情+财报增量；每日 07:00/23:00 研报抓取（下载一个即分析入库）

## 目录

```
agent/      LangGraph 意图路由图          skills/     fin_query / report / scanned_report
api/        路由 + JWT 鉴权中间件          tools/      爬虫 / 行情同步 / SQL guard / 视觉OCR
core/       配置 / 安全 / 双调度器          storage/    PG 连接池 + 全部 DDL
web/        React 前端（登录/问答/研报/看板） range_trading/  量化扫描子系统
```

分层调用：`api → agent → skills → tools → storage`。新 Agent 请先读 [AGENTS.md](AGENTS.md)。

## 快速开始

```bash
pip install -e .            # 安装依赖
cd web && npm install && npm run build   # 构建前端

cp .env.example .env        # 配置密钥（PG / Tushare / LLM / 爬虫 Cookie / 邀请码）

python -m uvicorn app:app --host 127.0.0.1 --port 8208   # 启动
```

数据回填（首次）：

```bash
python -m tools.market.sync_tushare      # 日线行情 2000-至今
python -m tools.market.sync_financial    # 财务三表 + 指标
```

首次启动用户表为空时自动创建种子管理员（见 `.env` 的 `AUTH_ADMIN_USER`）。

## 文档

| 入口 | 内容 |
|------|------|
| [AGENTS.md](AGENTS.md) | Agent / 新成员第一入口（全貌 + 踩坑清单） |
| [docs/README.md](docs/README.md) | 模块文档索引（登录 / 问答 / 研报 / 模型 / 安全 / 量化） |

## 知识星球使用说明

1. 浏览器登录 wx.zsxq.com，复制 Cookie 写入 `.env` 的 `ZSXQ_COOKIE`
2. 群组 ID 在星球页面 URL 中获取，写入 `ZSXQ_GROUP_ID`
3. 爬取频率已内置反检测延迟，请勿调低
