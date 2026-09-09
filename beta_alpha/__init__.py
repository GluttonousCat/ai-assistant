"""
beta_alpha — 产业链Beta / 个股Alpha 独立模块

自包含子系统 (对标 range_trading/): 种子链模板/分析能力/Skill编排/SSE流式/
DDL/测试全部在本包内, 对外只暴露稳定入口:
- beta_alpha.skills.beta.BetaSkill / beta_alpha.skills.alpha.AlphaSkill
- beta_alpha.streaming.stream_chain / stream_alpha (SSE 生成器)
- beta_alpha.analysis.chain_analysis (量化) / chain_extract (研报环节抽取)
- beta_alpha.schema.init_chain_extract_schema (DDL)

验证: pytest beta_alpha/tests ; 改链模板后跑 python -m beta_alpha.analysis.chain_analysis <chain_id>
"""
