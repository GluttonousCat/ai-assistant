# beta_alpha — 产业链Beta / 个股Alpha 子系统

自包含模块（对标 `range_trading/`）：模板、量化、Skill 编排、SSE、DDL、测试全部在本包内，
对外只暴露稳定入口，平台其余部分（api/agent/registry/研报钩子）只 import 本包顶层名。

## 结构

```
beta_alpha/
├── chains/               种子链模板 (*.yaml, 专业知识载体, 可手编扩展)
├── analysis/             原子能力层 (纯 SQL+pandas, 无 LLM)
│   ├── chain_analysis.py   环节->标的三源映射 + 环节指数/超额收益
│   └── chain_extract.py    研报正文 -> fin.chain_extract 环节抽取
├── skills/               Skill 编排层 (LLM 解析与综述)
│   ├── beta.py             BetaSkill (链解析->映射->综述)
│   └── alpha.py            AlphaSkill (预期差四象限)
├── prompts.py            全部提示词 (JSON 花括号 {{}} 转义)
├── schema.py             fin.chain_extract DDL (模块内 ensure)
├── streaming.py          SSE 编排 (stream_chain/stream_alpha, api 薄包装消费)
└── tests/                pytest (无 DB 依赖: 模板结构/分档规则/单位归一)
```

## 对外入口（平台接线点只认这些）

| 入口 | 消费方 |
|---|---|
| `beta_alpha.skills.beta.BetaSkill` / `alpha.AlphaSkill` | `agent/fin_graph.py`、`skills/registry.py`、CLI `python -m beta_alpha.skills.beta\|alpha` |
| `beta_alpha.streaming.stream_chain/stream_alpha` | `api/finance.py` SSE 分支（薄包装） |
| `beta_alpha.analysis.chain_analysis` CLI | 量化速览 `python -m beta_alpha.analysis.chain_analysis ai_compute` |
| `beta_alpha.analysis.chain_extract.spawn_chain_extract` | `skills/report/skill.py` 深度提取后钩子 |
| `beta_alpha.schema.init_chain_extract_schema` | 建表（幂等；`fin.fina_mainbz` 属 Tushare 同步族，DDL 仍在 `storage/pg_schema.py`） |

## 验证

```bash
pytest beta_alpha/tests                                  # 结构与规则
python -m beta_alpha.analysis.chain_analysis ai_compute   # 改链模板后必跑
python -m beta_alpha.skills.beta "AI算力产业链有哪些环节"  # 端到端(含LLM)
python -m beta_alpha.skills.alpha "中际旭创的预期差"
```

## 约定

- 分层契约不变：`skills/base.py` 的 `SkillContext`、`llm/client.py` 工厂、`storage/pg.py` 仍由上层框架提供
- 改分档阈值/单位归一/趋势判定：改纯函数（`assign_tier`/`to_yuan`/`classify_drift`）并补 tests
- 新增产业链：在 `chains/` 加 YAML（规范见 `.agents/skills/chain-beta/references/chain-template-guide.md`）
- 详细设计：`docs/agents/chain_beta_skill.md`
