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

---

## 审核记录 (总设计师 · 2026-09-25) — ✅ 审核完毕, 待合入 main (分支 research/T08)

- 11 例测试绿 (独立 worktree 实跑); `python -m skills.research --help` 实测正常。
- 亮点: 脚注编号按全文首次出现序且同引用复用同号; 幽灵引用原样保留 (不吞证据问题);
  flags → 文首"数据核查提示"块; Stage1/2 模块 lazy import, 未合并不阻塞 — 处理得当。
- **结论: 关单。** 待 T06/T07 合入后跑一次 `--skip-evidence --only render` 联调 (见新指令)。

### 新指令 (联调检查单, T06+T07 合入 main 后执行一次)
1. 构造或复用真实 draft.json → `python -m skills.research 贵州茅台 --skip-evidence --only render`
2. 核对: 每个脚注在来源区有对应条目; 图片路径 figs/ 可达; flags 块渲染正确。
3. 发现问题记入提交说明, 小问题可直接在自己文件内修, 大问题报 T00 状态板。