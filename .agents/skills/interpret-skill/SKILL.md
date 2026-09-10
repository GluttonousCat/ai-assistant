---
name: interpret-skill
description: 在本投研平台（Alpha Finance Radar）生成上市公司投研解读：量化画像十板块 + 巨潮年报原文语料（管理层讨论/风险章节提炼）+ 研报观点 + 十大股东，产出公众号级六模块图文文章，可联动 PPT。凡用户说"给XX做（一份）解读/深度解读/解读文章/写篇XX的文章/XX值不值得写一篇"且对象是上市公司时使用；用户要画像/体检时也用本技能（画像即解读第一步）。只写单一股票，多股票需求逐只确认。
---

# 投研解读（interpret-skill）

管线：**量化画像**（`content/profile.py` 十板块，纯数据零 LLM）+ **年报语料**（`content/annual_report.py`：`fin.cninfo_announcement` 定位 PDF → fitz 全文 → 章节切片 → extract LLM 中文提炼，全程降级）+ **配图五张**（`content/charts.py`，数据确定性渲染）+ **成文**（六模块 prompt，agent LLM）。设计文档：`docs/agents/interpret_agent.md`。

## 步骤

1. **确认标的**：用户话术须含股票名或代码；模糊时先确认，不要猜。

2. **数据预检（可选但推荐，缺啥补啥再解读）**：
   ```bash
   # 年报 PDF（解读语料的分量来源；无 PDF 会自动降级成纯画像模式）
   .venv/Scripts/python.exe -m tools.cninfo.downloader --stock 中芯国际 --years 2025
   # 十大股东（模块四筹码结构；已有数据则秒过）
   .venv/Scripts/python.exe -m tools.market.sync_holders --stock 中芯国际
   ```

3. **执行解读**（项目根目录，约 2~4 分钟，含两次 LLM）：
   ```bash
   .venv/Scripts/python.exe -m content interpret 中芯国际
   # 变体: --annual-year 2024 指定年报 | --no-annual 纯画像 | --ppt 联动出片
   ```
   产物 `output/articles/YYYYMMDD_gzh_<名>.md` + `assets/*.png`（图与 md 同目录引用）。
   返回 JSON 含 `chars/charts/annual_report(有|无)/ok_sections`。

4. **验收再交付**：
   - Read 产物 md：六模块齐全、`![](assets/...)` 占位符原样、券商观点带机构名、文末免责声明
   - 抽查 1~2 个数字与画像口径一致（金额换算成亿）
   - `annual_report: 无` 时如实告知用户降级原因（无 PDF/无文本层），不装作有年报视角

5. **表达边界**：文章是研究记录不是投资建议；量化信号（range_trading）不入文；券商观点只转述标注机构。

## 成功标准

- 六模块结构完整、五图位置正确、无编造数字、无整段搬运研报/年报
- 年报语料"有"时：模块一出现"年报表述"、模块五出现"年报自述"类标注
- 产物可直接粘贴公众号编辑器（md + 本地图）
