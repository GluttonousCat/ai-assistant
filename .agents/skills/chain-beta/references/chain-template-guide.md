# 产业链模板编写指南（chain-beta）

模板目录：`beta_alpha/chains/*.yaml`，一个文件一条链。加载即缓存（进程级），编辑后新进程生效；命令每次运行都是新进程，无需额外操作。

## 双通道建链（推荐路径）

**通道一：LLM 生成（forge 闭环）** —— 主题 → 草稿（喂真实披露语料样本）→ 结构校验 → 关键词命中率校验（vs 74.5 万行主营构成）→ 零命中自动修正一轮 → 落盘：

```bash
.venv/Scripts/python.exe -m beta_alpha.forge "磷化工产业链"          # 生成并保存
.venv/Scripts/python.exe -m beta_alpha.forge "苹果代工" --no-save    # 只看草稿
```

网页端同款：产业链页 →「＋ 新建链」弹窗（SSE 展示草稿/校验/修正各阶段，YAML 可编辑后保存）。

**通道二：手写** —— 复制 `beta_alpha/chains/_template.yaml` 为 `<chain_id>.yaml` 编辑（下划线开头不会被加载）。

**两通道共用的验证命令**（结构 + 每环节每关键词命中股票数 + 披露样例）：

```bash
.venv/Scripts/python.exe -m beta_alpha.forge --check <chain_id>
```

读法：绿色✓=该关键词命中 N 只；红色✗=主营构成零命中——英文缩写类（GPU/HBM）常只命中研报 tags、环节已有其他词命中时**不必处理**；**环节整体 0 只才必须改**（用更宽的产业词，但仍守泛词纪律）。

## YAML 结构

```yaml
chain_id: ai_compute          # 小写下划线, 即文件名
name: AI算力产业链             # 展示名, 也是用户话术匹配字段之一
desc: 一句话链条逻辑            # 供 LLM 选链与综述
drivers: [大模型训练, 云厂商资本开支]   # 驱动因素
nodes:
  - id: ccl                   # 小写下划线, 链内唯一
    name: 覆铜板CCL            # 展示名, 参与话术匹配
    keywords: [覆铜板, CCL]     # 主营构成/研报tags 匹配词 (ILIKE 子串)
    sw_l2: [元件]              # 申万二级兜底, 基础词 (见下)
edges:
  - [ccl, pcb]                # [上游, 下游], 只在已定义节点之间
```

## keywords 设计原则（最容易出错的地方）

匹配对象是 `fin.v_main_biz.bz_item`（公司主营构成项目名）与研报 tags/标题，ILIKE 子串、大小写不敏感。三条原则：

1. **产品口径 + 应用口径都给**。公司披露口径不统一：沪电股份不写"PCB"而写"企业通讯市场板/汽车板"，只给产品词会漏掉龙头。新链上线后看漏匹配公司的 bz_item 原文（见下方排查命令）再补应用词。
2. **警惕泛词误报**。真实踩坑：`整机` 命中"带式输送机整机"（运机集团混入服务器环节）、`树脂` 命中"粉末涂料树脂"（光华股份混入电子材料）。宁缺勿滥——泛词要加限定（`树脂`→`电子树脂/环氧树脂/特种树脂`）。
3. **英文缩写直接写**（PCB/GPU/HBM/CPO），ILIKE 不区分大小写。

误报不影响指数（占比不足进不了强/中档），但会污染标的清单——用户看到"汽车板簧"证据串会立刻发现，模板作者也要主动清。

## sw_l2 兜底规则

`sw_l2` 是节点在申万 L2 行业的兜底归属，只影响"中/弱"档判定（无主营证据时，行业命中 + 研报≥2 篇 → 中）。**用基础词、子串匹配**：写 `元件` 能命中 `元件Ⅱ`，写 `电机` 能命中 `电机Ⅱ`，写 `电源设备` 能命中 `其他电源设备Ⅱ`。可先查可用行业名：

```bash
.venv/Scripts/python.exe -c "from storage.pg import PgClient; import sys
with PgClient() as pg:
    print([r['industry_name'] for r in pg.fetch_all(\"SELECT industry_name FROM stock.index_classify WHERE level='L2' ORDER BY 1\")])"
```

## 编辑后的验证流程

1. YAML 语法检查（缩进/花括号；流式写法 `- {id: x, ...}` 可用）
2. 跑该链量化表：`.venv/Scripts/python.exe -m beta_alpha.analysis.chain_analysis <chain_id>`
3. 逐环节扫一遍头部标的：强档必须是主营真占比，弱档证据串无行业外误报
4. 漏匹配排查——看目标公司最新报告期主营构成原文（即披露口径）：

```bash
.venv/Scripts/python.exe .agents/skills/chain-beta/scripts/show_mainbiz.py 002463.SZ
```

（`--type I/D` 可切行业/地区维度。）关键词照输出补齐。

## 已知数据坑（模板作者必读）

| 现象 | 根因 | 处理 |
|---|---|---|
| 某环节强档数明显偏少 | 主营构成回填未轮到该股（按 ts_code 排序回填） | 等 `fina_mainbz` 回填完成（见 data-ops.md），或临时定向补拉 |
| 某龙头公司不匹配 | 披露口径差异（应用口径/业务线口径） | 查 bz_item 原文补关键词 |
| 指标列 NaN | 环节强+中成分 <2 只 | 正常降级；多为数据未覆盖而非 bug |
| 某股出现在多个环节 | 业务多元（如生益=覆铜板+PCB） | 正常，证据串会分别注明占比 |
