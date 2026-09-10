# 扫描件研报分析 Agent 设计

> Alpha Finance Radar 平台 · ScannedReportSkill（图片型 PDF 分析）
> 更新：2026-08-30

## 一、系统设计

### 1.1 问题与方案选型

知识星球研报中存在**图片型/扫描件 PDF**（无文本层，普通提取为 0 字符，曾导致"研报正文无内容"）。方案对比：

| 方案 | 说明 | 结论 |
|------|------|------|
| 本地 OCR (tesseract/PaddleOCR) | 免 API 成本，但中文金融表格识别差、需装重型依赖 | ❌ |
| **PDF → PNG → 多模态 LLM 识别** | PyMuPDF 渲染 + qwen3.8-flash 逐页识别，表格/数字保真由 prompt 保证 | ✅ 采用 |
| 整页直接问模型做分析 | 每次分析都要重发图片，成本高且不可复用 | ❌（仅 OCR 用视觉，分析用文本模型） |

**核心思路**：视觉识别只做一次（OCR），把扫描件**转成普通文本研报**，之后走统一的分析链路——视觉是工具，不是流程。

### 1.2 流水线

```
ScannedReportSkill (编排层)
  ① 校验: report_id → 有正文则直接分析; 无正文且 is_image_pdf 才走 OCR
  ② OCR:  tools/finance/pdf_vision.ocr_pdf
          PyMuPDF 渲染 PNG (dpi 130) → qwen3.8-flash 逐页识别
          prompt = SCANNED_OCR_PROMPT (金融研报版式知识: 表格转文本行/数字精确/跳过页眉页脚)
  ③ 回写: content_text = OCR 全文 → 从此与普通文本研报同构
  ④ 深度提取: 组合调用 ReportSkill.extract (评级/盈利预测/观点/tags 入库)
  ⑤ 综合解读: SCANNED_ANALYSIS_PROMPT (买方研究员视角, 文本模型)
```

### 1.3 解耦设计（重点）

| 层 | 文件 | 职责 | 变更频率 |
|----|------|------|---------|
| 视觉工具 | `tools/finance/pdf_vision.py` | is_image_pdf / ocr_page_png / ocr_pdf，零业务逻辑 | 低（换视觉模型只改 config） |
| 专业 prompt | `skills/scanned_report/prompts.py` | OCR 版式规则 + 分析视角，**专业知识载体** | 高（持续调优，不动代码） |
| 编排 skill | `skills/scanned_report/skill.py` | 流程串联，④组合 ReportSkill 而非复制其逻辑 | 低 |
| 常规链路复用 | `skills/report/skill.py _vision_backfill` | 常规提取发现无正文图片 PDF 时调**同一工具**自动回填 | — |

**可扩展方向**（设计预留）：prompt 按研报类型分化（个股/行业/宏观不同分析框架）、OCR 页数与 dpi 按需调、并发识别加速、表格结构化输出（直接吐 markdown 表）。

### 1.4 专业知识体现（prompt 设计）

- **OCR prompt**：金融版式感知——财务表格转可读文本行（`营业收入 | 2025A: 100亿 | 2026E: 120亿`）、数字/百分比/货币精确照抄、图表只取标签数值、跳过页眉页脚免责声明
- **分析 prompt**：买方研究员视角五节框架——投资要点/核心逻辑（业务驱动·竞争格局·量价拆分）/财务与估值（预测·估值方法·目标价推导）/催化剂/风险；要求区分事实与观点、数字标注报告期、OCR 噪音标 [存疑]

## 二、使用方式

```python
from skills.base import SkillContext
from skills.scanned_report.skill import ScannedReportSkill

ctx = ScannedReportSkill()(SkillContext(
    user_input="分析扫描件研报 642",          # 或 params={"report_id": 642}
))
print(ctx.result["summary"])                  # 专业解读
```

已注册 `SkillRegistry`（name=`scanned_report`），意图词：扫描件/扫描研报/图片PDF。

## 三、验证记录（2026-08-30）

- **#642 摩根士丹利-比亚迪（1211.HK）二季度**（9 页图片型 PDF）：
  - OCR：9/9 页成功，42,447 字，单页 15-40s（qwen3.8-flash）
  - 深度提取入库（analysis_status=done）
  - 解读质量示例：「2Q26 净利 82 亿元（环比+102%/同比+30%）…维持增持，目标价 121 港元，较收盘价有 32% 上行空间」+ 量价拆分/单车利润/竞争格局
- qwen3.8-flash 视觉输入可用性：实测正常（qwen3.8-flash-latest / qwen-vl-flash 不存在）

## 四、历史问题与处理记录

| 问题 | 根因 | 处理方案 | 状态 |
|------|------|---------|------|
| 扫描件研报"正文无内容" | 图片型 PDF 无文本层 | 本 Agent（PDF→PNG→视觉 OCR→回写） | ✅ 本轮 |
| 首版视觉用 qwen-vl-max 且仅在深度提取时兜底 | 模型路由未统一 | 视觉统一 qwen3.8-flash（见 llm_models.md）；OCR 抽成独立工具层 | ✅ 本轮 |
| 视觉实现散落两处（extractor 与 skill 重复） | 历史演进 | 统一收敛到 `tools/finance/pdf_vision.py`，两处调用同源 | ✅ 本轮 |
