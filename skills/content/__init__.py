# -*- encoding: utf-8 -*-
"""
content/ — content asset pipeline: data/reports -> profile -> WeChat article / PPT

- profile.py   listed-company profile (eight sections, pure data, reuses the MCP tool layer)
- article.py   WeChat official-account article (stock-profile mode / report-topic
               summary mode, built-in compliance disclaimer)
- ppt.py       profile -> pptgen task book -> ../pptgen slide generation
               (LLM config injected via environment variables)
- prompts.py   prompts (data discipline: no fabricated numbers; copyright
               discipline: views paraphrased with the institution credited)
- cli.py       python -m agent.content profile|article|ppt

Output directories: output/articles/ (WeChat markdown), output/ppt/ (task book + pptx)
"""
