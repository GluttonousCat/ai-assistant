---
name: ppt-generate
description: 生成/修改带真实动画的 PowerPoint 演示文稿（.pptx）。基于自研 LibreOffice MCP（pptgen），原子操作构建每一页，动效以 PowerPoint 原生 presetID 元数据导出，PowerPoint/WPS 打开即可播放。凡用户提到 做PPT、做一个演示文稿、PPT、幻灯片、deck、汇报片、路演材料、宣讲材料、把某内容做成 PPT、改 PPT、给 PPT 加动画/动效/切换，一律使用本技能——不需要用户说"pptgen"。
---

# PPT Generate Agent（LibreOffice MCP 驱动）

架构：**子智能体 + pptgen MCP**。`mcp__pptgen__*` 共 18 个原子工具（new_deck/add_slide/add_textbox/add_shape/add_table/add_image/add_animation/set_transition/render_slide/save…），底层是常驻 headless LibreOffice（命名管道桥），坐标单位 mm，16:9 页面 338.7×190.5。代码在 `pptgen/bridge/`，详见 `docs/agents/ppt_generate_agent.md`。

核心优势（对比文生图/HTML转PPT）：**改稿不抽盲盒**（每次修改是确定性工具调用，可增量改）；**动效是真的**（导出 OOXML `<p:timing>` + presetID，PowerPoint 动画面板可见可改）；**自查闭环**（render_slide 出 PNG → 看图 → 就地修）。

## 按需求选路径

| 用户意图 | 做法 |
|---|---|
| 新做一份 PPT（≥2 页的成品） | **派发子智能体**：用 Agent 工具（general-purpose），提示词 = `references/subagent_prompt.md` 全文 + 本次主题/要求/输出路径 |
| 小改（改文字/加一页/调动画） | 主 agent 直接调 `mcp__pptgen__*`：先 `open_deck` + `list_slides` 对齐状态再动手 |
| 只是问能做什么/动效有哪些 | 读 `references/animation_catalog.md` 回答 |
| 排障（起不来/挂了/渲染怪） | 读 `references/troubleshooting.md` |

## 主 agent 直接改稿的最小循环

```
open_deck(path) → list_slides() → (set_text / add_animation / …) → render_slide(i) → Read PNG 检查 → save(path)
```

## 关键约定

- **形状引用**：每个 add_* 返回 `name`，后续 set_text/add_animation/remove_animation 都用它；`list_slides` 随时可查全量。
- **动效三要素**：effect（枚举见工具描述：fly_in_left/fade/zoom_in/peek_in_left/wipe_in_top…）、trigger（onclick/with_previous/after_previous）、duration/delay（秒）。**禁止编造枚举值**，只能用工具描述里列出的。
- **工作流必须含自查**：每页做完 render_slide 并 Read 图片检查布局（溢出/重叠/字号），有问题当页修完再下一页；save 后向用户报路径。
- **首次调用慢是正常的**：冷启动 headless LibreOffice 需 20s-2min（之后常驻秒回）。工具超时先重试一次再报错。
- **兜底**：本会话若没有 `mcp__pptgen__*` 工具（MCP 未加载/新注册未重启），用 Bash 走 cli：`.venv/Scripts/python.exe -m pptgen.bridge.cli <op> --json '<参数JSON>'`，op 与工具同名，deck 状态在常驻 soffice 里跨调用保持。

## 硬约束

- 面向用户的输出一律简体中文；PPT 内文字默认微软雅黑
- 动效克制：每页 2-5 个，服务信息层次（标题先入、要点逐条、数据最后），不堆花样
- 数字类内容（统计/金额/日期）必须来自用户给的材料或明确让用户提供，禁止编造
- 完成后必须 `save` 到用户指定路径（默认建议 `pptgen/output/<主题>.pptx`），并报告绝对路径与页数
