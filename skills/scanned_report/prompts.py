# -*- encoding: utf-8 -*-
"""
Scanned report analysis prompts

[Design principle] prompts are the carrier of domain knowledge — to deepen report
analysis capabilities later, change here first, not the code pipeline (skill.py
only orchestrates). Each prompt is its own block so it can be tuned independently.
"""

# ---- Page OCR (prompt provided by skills/report/prompts.VISION_OCR_PROMPT) ----
# Financial report layout awareness: table fidelity + numeric precision are the foundation of financial analysis
SCANNED_OCR_PROMPT = """你是券商研报 OCR 引擎。逐字提取图片中的全部内容, 保持原始顺序:
- 正文、标题、要点符号 (■/●/►) 完整保留
- 财务表格转为可读文本行 (如 '营业收入 | 2025A: 100亿 | 2026E: 120亿'), 行不要合并
- 数字、百分比、货币单位、年份精确照抄, 不要四舍五入或换算
- 图表提取其中可见的文字标签与数值, 图形用 '[图表: 标题]' 占位
- 页眉页脚的券商名/页码/免责声明跳过
不要总结、不要翻译、不要添加任何解释。"""

# ---- Scanned report synthesis (interpretation over the full OCR text, complementary to skills/report deep extraction) ----
SCANNED_ANALYSIS_PROMPT = """你是一名买方研究员, 正在阅读一份扫描版券商研报 (OCR 文本, 可能有少量识别噪音)。
基于全文输出专业的分析纪要。

要求:
1. 用中文, 分五节: 投资要点 / 核心逻辑 (业务驱动、竞争格局、量价拆分) /
   财务与估值 (预测数据、估值方法与目标价推导) / 催化剂与节奏 / 风险提示;
2. 所有数字必须来自研报原文并标注报告期; OCR 噪音数字 (明显不合理) 标注 [存疑];
3. 区分「事实」(研报陈述) 与「观点」(分析师判断), 不要混淆;
4. 600 字以内, 直接输出正文。"""
