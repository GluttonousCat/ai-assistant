# 排障手册（pptgen）

## 症状 → 处置

| 症状 | 原因 | 处置 |
|---|---|---|
| 第一个工具调用 20s-2min 无响应 | headless LibreOffice 冷启动（首次/杀过后） | 等；client 超时 330s 自会报错，重试一次 |
| `soffice 管道桥建立失败` | 全新 profile 首启卡死（本机已知坑）或 soffice 僵死 | `cli shutdown` → 仍失败则 PowerShell 杀 `soffice.bin`（CommandLine 含 `pptgen/lo_profile` 的）→ 重试（worker 会自动清场+播种 profile） |
| 工具频繁 `DisposedException` | 上个客户端清理竞态 | worker 会自动重连重试；连续失败则 shutdown 后重来 |
| `page.Count` 类奇怪属性错误/操作半途失效 | 多客户端并发同一 daemon | 保证同一时间只有一个构建方（子智能体串行工作） |
| 渲染 PNG 偏小(682px) | impress_png_Export 固定行为，PixelWidth 不生效 | 已知限制；布局检查够用 |
| 动画在 LibreOffice 放映不完整 | set 节点+预设元数据的实现取舍 | 在 PowerPoint/WPS 验收（面向 PP 导出优先） |
| 中文显示为方块 | 目标机器无微软雅黑 | add_textbox 传 font 参数换系统字体 |
| pptx 打不开/修复提示 | 极少见；save 时文档损坏 | `shutdown` 清场 → open_deck 修复或重建 |

## 常用命令（项目根目录）

```bash
# 直连调试（op 与 MCP 工具同名）
.venv/Scripts/python.exe -m pptgen.bridge.cli status
.venv/Scripts/python.exe -m pptgen.bridge.cli list_slides
.venv/Scripts/python.exe -m pptgen.bridge.cli add_textbox --json '{"slide":0,"text":"hi","x":20,"y":20,"w":100,"h":15}'
.venv/Scripts/python.exe -m pptgen.bridge.cli shutdown

# 探测（属性名/动画树）
.venv/Scripts/python.exe -m pptgen.bridge.cli probe

# 测试
.venv/Scripts/python.exe -m pytest pptgen/tests -v

# 演示
.venv/Scripts/python.exe -m pptgen.examples.demo_deck
```

## 进程/文件地图

- MCP server: `.zcode/config.json → mcp.servers.pptgen`（venv python + `pptgen/bridge/server.py`）
- UNO worker: LibreOffice 自带 python 跑 `pptgen/bridge/uno_worker.py`（**禁止**换系统 python 跑）
- soffice: `soffice.bin --headless`，profile `%LOCALAPPDATA%\pptgen\lo_profile`（从用户 GUI profile 播种）
- 渲染目录: `%TEMP%\pptgen_renders\`
- 环境变量: `PPTGEN_LO_PROGRAM`（LibreOffice program 目录）、`PPTGEN_PROFILE_DIR`

## 本机三大坑的由来（改环境前必读）

1. **必须直跑 soffice.bin**：经 soffice.exe 启动器时 `--accept` 不生效（socket/pipe 都不建）
2. **profile 播种**：全新 profile 的 headless 首启死锁（registrymodifications.xcu 永不生成）→ 从 `%APPDATA%\LibreOffice\4\user` 复制
3. **命名管道而非 socket**：本机 socket acceptor 不监听（原因不明），pipe 秒连
