# PPT Generate Agent（pptgen：LibreOffice MCP + 子智能体）

> 2026-09-06 首版。通用 PPT 生成器（与投研平台解耦），复刻「LibreOffice MCP 挂载到 Agent + 专职子智能体」
> 路线：改稿不抽盲盒（确定性原子操作）、动效是真的（OOXML presetID 直出）、渲染自查闭环。

## 一、系统设计

```
用户"做个PPT" → skill「ppt-generate」(.agents/skills/) 触发
 → 主 agent 派发 general-purpose 子智能体（references/subagent_prompt.md 完整提示词）
 → 子智能体调 MCP 工具 mcp__pptgen__*（18 个，.zcode/config.json 注册，stdio）
     server.py（venv Python3.12 + fastmcp）
       ↕ JSON-lines（id/op/args，protocol.py）
     uno_worker.py（LibreOffice 自带 Python 3.10 + pyuno，版本强绑定禁混用）
       ↕ UNO 命名管道 uno:pipe,name=pptgen_pipe
     soffice.bin --headless（独立 profile %LOCALAPPDATA%\pptgen\lo_profile）
 → 每页 render_slide 出 PNG（%TEMP%\pptgen_renders）→ 子智能体 Read 自查 → 修正
 → save .pptx（<p:timing> + presetID/presetClass/presetSubtype，PowerPoint/WWS 动画面板可见可改）
```

代码：`pptgen/bridge/`（protocol 路径发现 / uno_worker UNO执行器 / client / cli 调试兜底 /
presets 效果目录 / server MCP）。测试 `pptgen/tests/`（6 用例端到端），示例 `pptgen/examples/demo_deck.py`。
兜底链路：MCP 不可用时 `python -m pptgen.bridge.cli <op> --json '{...}'`（op 同名，deck 状态活在常驻 soffice）。

### 动画实现（核心）

旧版形状 `Effect` 属性路线已实测否决（导出 pptx 不生成 `<p:timing>`）。现行方案是**纯 SMIL 节点树构建**：

- 结构（回灌实验实测约定）：main_sequence(SequenceTimeContainer, node-type=4) → outer par(Begin=indefinite/延时) → inner par → AnimateSet(Target=形状, AttributeName="Visibility")
- UserData（**NamedValue 序列，不是 PropertyValue**）：node-type(int 1/2/3=触发)、preset-class(int 1=entrance/2=exit)、preset-id("ooo-entrance-fly-in"等)、preset-sub-type("from-left"等字符串，导出器翻译为 PP 数字)
- preset-id 目录 60+ 项由回灌实验收割（注入 PP presetID 1-45 → 读回 LO 命名）
- 触发：onclick=新组(Begin=indefinite)；with_previous=复用末组(Begin=delay)；after_previous=新组(Begin=delay)

## 二、需求优化

- 通用定位（用户决策）：Skill 沉淀通用排版/动效方法论；投研场景模板留二期
- 不做的（一期）：原生图表（用图片插入）、.potx 母版、按段落分条动画
- 已知限制如实告知：渲染 PNG 固定 682px 宽；Impress 内放映为简化行为（PowerPoint/WPS 完整）

## 三、问题（踩坑实录，全部本机实测）

| # | 坑 | 现象 | 根因 |
|---|---|---|---|
| 1 | soffice.exe 启动器 | `--accept` 声明的 socket/pipe 都不建立，冷启动假死 | Windows 启动器处理 accept 参数的缺陷 |
| 2 | 全新 profile 首启死锁 | headless 下 registrymodifications.xcu 永不生成，实例僵死 | 首启向导在 headless 下挂起 |
| 3 | socket 不监听 | 本机 socket acceptor 从不 bind | 未明；命名管道正常 |
| 4 | resolve() 无限阻塞 | office 初始化期间 UnoUrlResolver 永不返回 | 桥接无超时 |
| 5 | profile 锁竞争 | 上实例优雅退出未完时下实例冷启动卡死 | 独立 profile 也有锁 |
| 6 | 旧版 Effect 不导出 | shape.Effect 设了但 pptx 无 timing | sd 导出器只认内存 SMIL 树 |
| 7 | 本地/办公端枚举不一致 | pyuno 本地认 FADE_IN，setPropertyValue 拒绝 | 两套类型注册表不同 |
| 8 | UserData 类型拒绝 | PropertyValue 赋值报 CannotConvert | XAnimationNode.UserData 是 sequence&lt;NamedValue&gt; |
| 9 | 中文样式静默失效 | 只设 CharHeight 时中文回落 18pt 默认值 | 中文走 CharHeightAsian 系 |
| 10 | fade 切换导出为空 | (37,0) 映射导出后 transition 无子元素 | LO 内部 fade 子类型是 101 |
| 11 | 渲染分辨率 682px | FilterData PixelWidth 不生效 | impress_png_Export 固定行为 |
| 12 | 子包名遮蔽 | pptgen/mcp/ 遮蔽官方 mcp 包致 fastmcp 崩 | 命名冲突，改名 bridge/ |

## 四、处理方案（与上表对应）

1. 直接 Popen `soffice.bin`（不走启动器）
2. profile 播种：无 registrymodifications.xcu 时从 `%APPDATA%\LibreOffice\4\user` copytree（排除锁/缓存）
3. 一律命名管道桥 `--accept=pipe,name=pptgen_pipe;urp;`
4. `_resolve_with_timeout` 看门狗线程（15s 弃置重试，冷启动等待上限 300s）
5. 生命周期固化：`shutdown`/EOF 时 terminate→限时强杀→确认消失；spawn 前清场同 profile 残留实例；断桥自动重连重试一次
6. 改为手工构建 SMIL 树（见系统设计）
7. preset 目录以办公端 setPropertyValue 实测为准（本地注册表仅参考）
8. `_set_userdata` 用 NamedValue
9. `_style_text` 字号/加粗三系同设（Western/Asian/Complex）+ 段落级 ParaAdjust + `TextHorizontalAdjust` 形状锚定
10. presets.py fade=(37,101)（反向导入实验定位）
11. 记录为已知限制（Logical=整页 1/100mm 亦不改善）
12. 子包更名 `pptgen/bridge/`

## 五、验证记录

- 2026-09-05/06 实测通过：
  - `pytest pptgen/tests`：6/6（内容工具/动画构建+XML断言/切换读回/移除/重开往返）
  - MCP stdio 协议端到端（fastmcp Client → server.py → worker → soffice）：18 工具全链路
  - pptx 解剖：presetID=2(fly)+presetSubtype=8(from-left)/10(fade)/23(zoom)/12(peek)、nodeType 三种、`<p:fade/>` 切换，均真实导出
  - 子智能体派发实测（cli 兜底路径）：3 页中文 deck 独立完成，含渲染自查与像素测量，并顺带修复坑 9/10
  - 人工验收项：`pptgen/examples/demo.pptx` 与子智能体成品在 PowerPoint/WPS 打开播放动画
- 冷启动耗时：全新 profile 首次 ~2-5min；常驻后 cli 单命令 ~2-4s

## 维护提示

- 改效果目录 → `pptgen/bridge/presets.py`（新增 preset-id 需回灌实验验证导出）
- LibreOffice 升级后首跑注意坑 1/2/3 是否复现（`cli probe` 排查）
- 改 worker 后跑 `pytest pptgen/tests`；skill 话术调整保持与工具描述里的枚举一致
