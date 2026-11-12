"""
beta_alpha — industry-chain Beta / single-stock Alpha standalone module

Self-contained subsystem (mirrors range_trading/): seed-chain templates, analysis
capabilities, skill orchestration, SSE streaming, DDL and tests all live inside
this package; only stable entry points are exposed:
- beta_alpha.skills.beta.BetaSkill / beta_alpha.skills.alpha.AlphaSkill
- beta_alpha.streaming.stream_chain / stream_alpha (SSE generators)
- beta_alpha.analysis.chain_analysis (quant) / chain_extract (report segment extraction)
- beta_alpha.schema.init_chain_extract_schema (DDL)

Verification: pytest beta_alpha/tests ; after editing a chain template run
python -m agent.beta_alpha.analysis.chain_analysis <chain_id>
"""
