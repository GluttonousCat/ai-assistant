# -*- encoding: utf-8 -*-
"""
content/ — 内容资产管线: 数据/研报 -> 画像 -> 公众号文章 / PPT

- profile.py   上市公司画像 (八大板块, 纯数据, 复用 MCP 工具层)
- article.py   公众号文章 (股票画像模式 / 研报主题综述模式, 合规免责内置)
- ppt.py       画像 -> pptgen 任务书 -> ../pptgen 出片 (LLM 配置环境变量注入)
- prompts.py   提示词 (数据纪律: 禁编数; 版权纪律: 观点转述标注机构)
- cli.py       python -m agent.content profile|article|ppt

产出目录: output/articles/ (公众号 md), output/ppt/ (任务书 + pptx)
"""
