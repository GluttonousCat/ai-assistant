# Alpha Finance Radar

个人智能投研平台 —— Tushare 财务数据 × 知识星球研报 × Agent 分析。

## 功能

- **用户系统**：JWT 登录注册（邀请码门槛），三级角色 —— 超级管理员（用户管理）/ 管理员（实时监控）/ 研究员
- **Agent 问答**：自然语言 → 意图路由 → Text-to-SQL 财务查询 / 研报解读，SSE 流式打字机输出
- **研报中心**：知识星球研报自动抓取入库，LLM 提取机构/标的/行业/地区/市场 + 评级与盈利预测，
  支持扫描件 PDF（视觉 OCR）、去重合并、多市场筛选
- **数据调度**：交易日 17:00 同步日线、分钟行情和财报并刷新看板；交易日盘中四轮更新 60F 观察。研报每日 00:00 和 12:00 采集，逐篇分析入库。

## 目录

```
api/         HTTP 路由、鉴权、SSE
core/        配置、路径、日志与安全
harness/     Agent 意图、循环、会话、模型路由与 MCP
skills/      行情、量化、财务查询、研报与研究方法
tools/       行情及文档数据源、查询和解析工具
storage/     PostgreSQL、SQLite、仓储查询与 DDL
workflows/   market 行情、reports 研报、runtime 服务、quant 量化、diagnostics 自检
jobs/        旧版命令兼容入口
tests/       Python 离线回归与真实库集成测试
evals/       Agent 评测语料、基准和评测命令
web/         React 前端及其 Node 测试
```

分层调用：`api → harness(引擎)+skills(领域) → tools → storage`。新 Agent 请先读 [AGENTS.md](AGENTS.md)。

量化命令在 [workflows/quant](workflows/quant)。`inputs` 准备数据，`backtests` 运行固定回测，`studies` 保存各项研究的独立入口，威科夫系列归在 `studies/wyckoff`。例如，`python -m workflows.quant.backtests.mtf --help` 可查看多周期回测参数。旧版 `python -m jobs.*` 命令仍可使用。

## 快速开始

在项目根目录使用 Python 虚拟环境。常规设置在 `config.yaml`，数据库连接、数据源凭据、模型密钥和邀请码放在本机 `.env`。已有部署继续使用原配置和数据目录。

```powershell
python -m pip install -e ".[dev]"
npm --prefix web ci
npm --prefix web run build
ai-assistant --host 127.0.0.1 --port 9001
```

现有启动脚本可继续使用 `python run_server.py`。部署使用仓库内已构建的 `web/dist`。离线回归运行 `python -m pytest`，真实库测试运行 `python -m pytest -m integration`，前端测试运行 `npm --prefix web test`。

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
| [docs/README.md](docs/README.md) | 模块文档索引（登录 / 问答 / 研报 / 模型 / 安全） |

## 知识星球使用说明

1. 浏览器登录 wx.zsxq.com，复制 Cookie 写入 `.env` 的 `ZSXQ_COOKIE`
2. 群组 ID 在星球页面 URL 中获取，写入 `ZSXQ_GROUP_ID`
3. 爬取频率已内置反检测延迟，请勿调低
