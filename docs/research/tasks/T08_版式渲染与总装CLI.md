# T08 · 版式渲染与总装 CLI (Stage 3)

> 先读 T00。你独占: skills/research/render/render_md.py (+ render/__init__.py), cli.py, __main__.py

## 目标
draft + figures → 可发布的研究报告 MD (证据脚注可溯 + 假设附录 + 免责); 一条命令总装全流程。

## 实现要点
1. render_md.py: `render_report(draft, facts, views, figs, out_path) -> Path`
   - 标题区: 公司名/代码/生成日期/证据统计 (N facts from X 来源, M views from K 机构)
   - 章节: body_md 中的 [E:eid]/[V:vid] 转脚注标 `<sup>[n]</sup>`; 文末"证据与观点来源"区
     按出现序展开: facts → 来源描述; views → `机构(日期): 主张 [report_id]`。
   - 图片: 按 T07 返回的 (path, chapter_no) 在对应章末插入 `![](figs/xxx.png)`。
   - flags 非空时在文首渲染"数据核查提示"块 (透明化, 不隐藏问题)。
   - 附录: 假设表 (陈述/验证指标/状态) + 方法论说明 (三层分离一段) + 免责声明。
2. cli.py 总装编排 (顺序函数, 图思维不引框架):
   collect 三源 (T02/T03/T04) → generate (T06) → figures (T07) → render;
   支持 `--skip-evidence` (复用已落盘分片) 与 `--only render`。
   一键: `python -m skills.research 贵州茅台`
3. pytest: fixture draft 渲染, 断言脚注编号与来源区一一对应 (离线)。

## 验收
茅台一键产出 output/research/600519.SH/report_<日期>.md;
文内每个脚注在来源区有对应条目; 图片路径正确。

## 禁止
改 Stage1/2 模块 (只调用); 渲染时"顺手润色"正文 (呈现层不改内容层)。
