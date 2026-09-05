# 动效目录与搭配建议

> 完整枚举以 `add_animation` / `set_transition` 工具描述为准（代码源 `pptgen/bridge/presets.py`）。
> 下方"PP"列为导出后 PowerPoint 侧等价预设（presetID），已实测验证。

## 进场（entrance）

| 效果 | PP 等价 | 语义/建议 |
|---|---|---|
| `appear` | Appear(1) | 瞬时出现；批量数据行 |
| `fade` | Fade(10) | 万能款；表格/图片/收尾元素 |
| `fly_in_left` 等 8 方向 | Fly In(2) | 方向语义=从哪进来；标题、大板块常用 left/top |
| `peek_in_left/top/...` | Peek In(12) | 轻量滑入；要点逐条首选 |
| `wipe_in_top/...` | Wipe(22) | 揭示感；进度/时间线 |
| `zoom_in` | Zoom(23) | 强调；核心数字/结论 |
| `crawl_in` | Crawl In(7) | 缓慢爬入；庄重场合 |
| `blinds` / `checkerboard` / `circle` / `diamond` / `box` / `plus` / `wheel` | 对应经典预设 | 分格揭示；少用，易显花哨 |
| `grow_turn` / `rise_up` / `float` / `ease_in` / `spin_in` / `stretch` / `random_bars` | 对应经典预设 | 点缀用 |
| `dissolve` | Dissolve In(9) | 颗粒感；配合深色背景 |

## 出场（exit，慎用）

`fade_out`(10) / `disappear`(1) / `zoom_out`(23) / `fly_out_*`(2) / `wipe_out_*` / `peek_out_*` /
`collapse` / `sink_down` / `swish` / `crawl_out` / `wheel_out` / `swivel_out` / `split_out` / `dissolve_out`

叙事上只建议"先亮后隐"型对比页（如旧方案 fade_out → 新方案 fade）。

## 触发与节奏

- `onclick`：翻页内分步讲解（默认）
- `with_previous`：同组齐动（标题+副标题），配 `delay` 错峰 0.1-0.3s 更自然
- `after_previous`：自动连播，要点逐条揭示的主流选法，`delay` 0.1-0.15s
- 时长：标题/大块 0.6-0.8s，要点 0.4-0.5s；同一页内统一节奏

## 页面切换

`fade`(37) 全篇统一最稳；`push_from_left/top/...`(35) 有推进感；`slide_from_*`(36) 轻滑；
`wipe_from_*`(1)、`dissolve`(40)。时长 0.5-0.8s。封面→目录可用一次 push 制造"开场感"，其余页统一 fade。

## 已知边界（如实告知用户）

- Impress 内放映=简化出场（节点是 set 行为），**PowerPoint/WPS 放映与动画面板完整**（预设引擎接管）
- 出场方向子类型个别值导出时可能省略（用默认方向）
- 单形状多段分条动画（按段落）未支持；要分条就拆多个 textbox
