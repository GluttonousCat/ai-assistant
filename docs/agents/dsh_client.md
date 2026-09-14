# dsh (DeepSeek Harness) 作为 MCP 客户端接入

> 更新:2026-09-14 · 一次性验证记录, 非依赖项 — 本项目**不引入 dsh 框架**,
> 仅把平台的 22 工具 MCP server (`python -m harness.mcp.server`) 当作外部 harness
> 的数据后端试用/评测。

## 一、系统设计(为什么这样接)

DSH 的范式是 "Agent = Model + Harness, 一切皆插件"。对本项目的正确用法
不是重写 core(已验证: 技术栈不接、单用户平台无热插拔需求), 而是**消费关系**:
dsh 这类 MCP 客户端 harness 直接连我们的 stdio MCP server, 复用平台全部
只读数据能力(A 股财务/行情/研报/产业链/量化扫描)。

```
dsh (TS, 插件化 harness)  ──stdio JSON-RPC──>  python -m harness.mcp.server (22 工具)
                                                    └─> skills/tools/storage (PG/SQLite)
```

- 默认只读:server 端 `ALPHA_MCP_READ_ONLY=1`(缺省)隐藏并拒绝写类工具
  (forge_chain / write_article 等), 外接任何客户端都不放大权限
- 需要放开生成类工具时设 `ALPHA_MCP_READ_ONLY=0` 再启动(自担风险)

## 二、接入配置(通用 MCP 客户端)

任何支持 stdio MCP server 的客户端(dsh / Claude Desktop / Cursor)配置要素一致:

| 项 | 值 |
|----|----|
| command | `<项目根>\.venv\Scripts\python.exe` |
| args | `["-m", "mcp.server"]` |
| cwd(如支持) | 项目根(定位 config.yaml/.env) |
| env | `PYTHONIOENCODING=utf-8`(Windows 控制台编码坑) |

dsh 处于 developer preview, MCP 客户端配置的具体文件位置/格式以官方文档为准
(github.com/deepseek-ai/deepseek-harness → docs);上述四要素是协议层的全部要求,
握手契约见下方冒烟脚本——它能跑通, dsh 就能连上。

## 三、验证记录(2026-09-14)

```bash
.venv\Scripts\python.exe jobs\smoke_mcp_stdio.py
```

脚本模拟外部客户端走完整线级握手: initialize → notifications/initialized →
tools/list → tools/call, 校验:

1. 协议版本与服务名回显(`alpha-finance-radar-mcp`)
2. 只读清单:19 个 read_only 工具可见, `forge_chain` 等写类不可见
3. 真实调用 `resolve_stock` 返回 `ok=true` 信封
4. stdout 纯净性:非 JSON-RPC 行直接报错(server 已把模块 print 重定向 stderr)

实测通过(本机 PG 可用时);进程内协议行为另见 `harness/mcp/tests/test_server.py`。

## 四、边界与后续

- dsh 尚在 preview, 配置面可能破坏性变更——本文只锚定 MCP 协议契约, 不锚定 dsh 配置格式
- 快车道/域子Agent/会话记忆是本项目 harness 的增值, 外接 dsh 时这些不复用
  (dsh 用自己的 loop);适合做**评测对照**(同一工具集在不同 harness 下的表现)
- 轨迹对照:平台侧已有 logs/trajectories/*.jsonl 全量轨迹, dsh 侧用其 Trajectory 视图
