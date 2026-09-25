# -*- encoding: utf-8 -*-
"""skills.research.compose — Stage 2 内容生成 (解释层+判断层)。

generator.py       T06 草稿生成器: 证据包 → 十章草稿 draft.json (LLM 逐章, 非流式)
                    提示词库直接 import T05 (prompts_chapters)
citation_check.py  T06 引用校验器 (纯逻辑, 离线可测): 数字句启发/幽灵引用/观点句规范
figures.py         T07 图表工场 (另一任务, 非本包初始内容)
"""
