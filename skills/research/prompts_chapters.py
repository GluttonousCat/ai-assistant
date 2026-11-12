# -*- encoding: utf-8 -*-
"""
Research production line Stage 2 · ten-chapter company-research template library (T05)

Methodology source: docs/research/company_industry_guide.md section 2 “公司研究的完整章节结构”
(the 10 chapter definitions transcribed one by one; the guide's wording is authoritative).
This file is template data + pure string-assembly functions: no LLM calls, no heavy
imports, company-agnostic (hardcoding company data is forbidden).

Consumer (T06, compose/generator.py):
  from skills.research.prompts_chapters import CHAPTERS, chapter_prompt
  prompt = chapter_prompt(ch, facts_digest, views_digest)   # fed to the LLM chapter by chapter

Field conventions (per chapter dict, six fields):
  no           chapter order 1..10 (aligned with Draft.chapters[].no)
  title        chapter title (verbatim from the guide)
  question     the chapter's research question (the guide's “回答”)
  wants        evidence categories needed; values from the closed set EVIDENCE_CATEGORIES,
               order = priority; T06 takes the union via the EVIDENCE_CATEGORIES matching
               rules, digests it, and injects it
  structure    structural skeleton (covered in order; includes the guide's “输出” artifact
               hints; figure artifacts are rendered by the T07 figure factory)
  length_hint  length hint (default; T06 may override based on evidence volume)

Chapter that produces the research-hypotheses JSON: HYPOTHESES_CHAPTER_NO = 8 (future
growth and scenario analysis). Note: the T06 task doc says “第10章(情景与风险)” — a
chapter-number typo: in the guide scenario analysis is chapter 8 and risk & counterevidence
is chapter 9; the native home of hypotheses (hypothesis + verification indicators) is
chapter 8; this constant is authoritative.
"""
from __future__ import annotations

from typing import Dict, List, Union

from skills.research.prompts_style import STYLE_PROMPT

# ---------- evidence-category vocabulary (closed set for wants; matching rules implemented by T06 filtering) ----------
# Semantics are "union": a chapter receives the merge of every matching category's evidence; mainbiz ⊂ profile, listing both is harmless.
EVIDENCE_CATEGORIES: Dict[str, str] = {
    "profile": "Tushare 画像板块: eid 前缀 “T:profile.” (公司基础/行业归属/股东/估值水位等静态属性)",
    "mainbiz": "主营构成明细: eid 前缀 “T:profile.mainbiz.” (分产品/分地区收入与构成)",
    "financials": "Tushare 财务时序: eid 前缀 “T:tushare.” (三表关键指标/财务指标多年时序)",
    "annual": "年报原子证据: eid 前缀 “A:” (经营数据/分部明细/管理层论断/风险条目)",
    "views": "研报观点: vid 前缀 “R:” (viewpoint/forecast/rating/risk, 永不作事实句)",
}

# ---------- ten-chapter templates (company research; the methodology guide's wording is authoritative) ----------
CHAPTERS: List[Dict] = [
    {
        "no": 1,
        "title": "公司定位与研究结论摘要",
        "question": "这家公司到底是什么? 当前最重要的经营变量是什么?",
        "wants": ["profile", "mainbiz", "views"],
        "structure": [
            "一页公司画像: 主营业务及收入来源 / 产业链位置(上游·制造·设计·下游) / 主要业务与客户结构",
            "核心经营驱动因素: 当前最重要的经营变量及其作用机制 (机制解释, 非标签罗列)",
            "需要进一步验证的关键假设 (点到为止, 详见后续各章)",
            "3~5个核心研究问题 — 后续各章依次回答的问题清单",
        ],
        "length_hint": "600-900字",
    },
    {
        "no": 2,
        "title": "商业模式与业务拆解",
        "question": "公司通过什么方式创造收入和利润?",
        "wants": ["mainbiz", "annual", "profile", "views"],
        "structure": [
            "业务树: 主要产品与服务, 各自收入贡献与商业模式说明",
            "客户结构: 客户是谁、采购动机、集中度与粘性",
            "收入构成: 按产品/地区/业务分部拆解 (收入构成图素材)",
            "交易与定价: 交易模式、定价方式、订单周期",
            "成本构成与主要经营杠杆 (业务驱动因素表素材)",
        ],
        "length_hint": "800-1200字",
    },
    {
        "no": 3,
        "title": "产业链位置与竞争格局",
        "question": "公司在价值链上处于什么位置? 客户为什么选择它?",
        "wants": ["annual", "views", "profile"],
        "structure": [
            "产业链图谱: 上游(原材料/设备/能源及供应商) → 公司环节(制造/研发) → 下游(客户/应用领域/终端需求)",
            "竞争对手与替代方案对照 (对照表素材)",
            "竞争机制分析: 技术/规模/客户认证/供应链等竞争因素 — 客户为什么选择它, 壁垒在哪",
            "竞争格局的变化因素: 什么会改变当前格局",
        ],
        "length_hint": "800-1200字",
    },
    {
        "no": 4,
        "title": "历史经营与财务质量",
        "question": "公司过去的增长和利润来自哪里? 经营质量如何?",
        "wants": ["financials", "annual"],
        "structure": [
            "5~10年历史财务面板: 收入/毛利/营业利润/净利润趋势 (趋势图素材)",
            "现金流与利润的匹配: 经营现金流对净利润的覆盖、资本开支节奏",
            "资产质量与营运资本: 资产负债结构、应收账款、存货、合同负债",
            "回报与效率: ROE / ROIC / 资产周转率 (注明口径)",
            "投入侧: 研发投入、资本开支、折旧摊销",
            "经营质量小结: 过去的增长和利润来自哪里, 质量是否可持续",
        ],
        "length_hint": "900-1400字",
    },
    {
        "no": 5,
        "title": "经营驱动因子与盈利机制",
        "question": "哪些变量真正决定公司的收入和利润?",
        "wants": ["annual", "financials", "mainbiz", "views"],
        "structure": [
            "经营驱动树: 收入拆为 量(产能×利用率×销量/出货) × 价(单价/产品结构), "
            "成本拆为变动与固定 (变量关系图素材)",
            "成本侧机制: 原材料/设备/人工/折旧的构成与变化",
            "传导机制: 资本开支 → 产能释放 → 收入 的时滞与前提条件",
            "规模效应与固定成本吸收",
            "关键指标监控表: 增长/产能/产品/盈利/现金流/资产/研发/资本回报 维度",
        ],
        "length_hint": "800-1200字",
    },
    {
        "no": 6,
        "title": "技术、研发与竞争优势",
        "question": "公司的竞争优势是否具有持续性?",
        "wants": ["annual", "financials", "views"],
        "structure": [
            "技术路线与产品能力概览",
            "竞争指标: 工艺/良率/性能/成本 等可比维度 (技术路线表素材)",
            "研发投入与技术成果: 投入→成果→收入的转化证据链",
            "壁垒证据表: 客户认证/转换成本/规模效应/供应链能力/产能布局",
            "优势持续性: 哪些壁垒可能被侵蚀, 用什么信号观测",
        ],
        "length_hint": "700-1100字",
    },
    {
        "no": 7,
        "title": "管理层、治理与资本配置",
        "question": "公司如何进行资源配置和战略执行?",
        "wants": ["annual", "financials", "profile", "views"],
        "structure": [
            "股权结构与治理结构 (含关联交易情况)",
            "资本配置档案: 资本开支/并购/融资/分红的历史与逻辑",
            "战略执行: 管理层披露的战略目标与执行进度对照; 重大项目及产能建设计划",
            "审计意见、内控与潜在治理风险 (重大事项追踪表素材)",
        ],
        "length_hint": "600-1000字",
    },
    {
        "no": 8,
        "title": "未来增长与情景分析",
        "question": "未来的经营变化可能通过哪些路径发生?",
        "wants": ["views", "annual", "financials"],
        "structure": [
            "增长路径盘点: 产能扩张/价格/产品结构 + 新客户/新产品/新市场 + 行业供需与周期变量",
            "基准/上行/下行情景: 每个情景的驱动假设与触发条件",
            "每个情景的可验证指标 (观测数据+时点+方向)",
            "外部观点对照: 机构预测作情景参照, 注明机构与日期, 不作事实",
            "本章识别的研究假设 — 除行文外, 另按文末约定追加输出 JSON",
        ],
        "length_hint": "700-1100字",
    },
    {
        "no": 9,
        "title": "风险与反证分析",
        "question": "什么情况会使当前研究假设失效?",
        "wants": ["annual", "views", "financials"],
        "structure": [
            "风险清单: 行业需求下滑/价格竞争、产能投放不及预期/利用率不足、技术路线变化、"
            "客户集中度、供应链/政策/汇率/融资、财务数据异常与披露口径变化",
            "预警条件: 每条风险配可观测的触发信号",
            "反证证据表: 现有证据中与主要假设冲突或构成限制的部分",
            "研究假设失效条件汇总 (呼应第1章关键假设)",
        ],
        "length_hint": "600-1000字",
    },
    {
        "no": 10,
        "title": "估值与持续跟踪",
        "question": "如何把经营假设映射到估值分析, 并持续验证?",
        "wants": ["views", "financials", "profile"],
        "structure": [
            "估值方法与适用条件选择 (口径说明)",
            "关键估值假设及敏感性",
            "同业对照的口径差异提示 (未经核对不直接横比)",
            "估值纪律: 估值是独立的分析模块, 不让预设股价结论反过来驱动业务事实的解释",
            "持续跟踪: 后续财报/公告/经营指标跟踪清单 + 关键事件监控 + 研究结论更新记录",
        ],
        "length_hint": "600-900字",
    },
]

_CHAPTER_INDEX: Dict[int, Dict] = {c["no"]: c for c in CHAPTERS}

# ---------- appended research-hypotheses JSON output (chapter 8; see the chapter-number note in the module docstring) ----------
HYPOTHESES_CHAPTER_NO = 8

# Note: this is a plain string (not an f-string / not a .format template); JSON braces are kept verbatim
# (AGENTS.md pitfall: JSON braces inside .format templates must be escaped as {{}} — a template engine is deliberately not used here).
_HYPOTHESES_REQUEST = """
## 追加输出 · 研究假设 JSON
在正文之后追加一个 ```json 围栏代码块, 输出本章识别的研究假设数组 (供下游结构化回填):
[{"statement": "假设陈述(一句话)", "indicators": ["验证指标(含数据来源提示)"], "related_eids": ["支撑/相关证据 id"]}]
规则: 每条假设必须可验证 (indicators 至少 1 条非空); related_eids 只用上文摘要中出现过的 id;
除该 JSON 块外, 正文不受影响。"""


def chapter_prompt(
    ch: Union[Dict, int],
    facts_digest: str = "",
    views_digest: str = "",
) -> str:
    """Assemble a single chapter's generation prompt (pure string assembly, no LLM call).

    ch             chapter dict from CHAPTERS (also accepts a chapter number int, auto-looked-up)
    facts_digest   fact-evidence digest text (built by T06; recommended one “[eid] 内容” line per item)
    views_digest   research-report view digest text (same shape; each line carries vid/org/date)
    """
    if isinstance(ch, int):
        ch = _CHAPTER_INDEX[ch]
    lines: List[str] = [
        STYLE_PROMPT,
        "",
        f"# 第 {ch['no']} 章 · {ch['title']}",
        "",
        "## 本章研究问题",
        ch["question"],
        "",
        "## 结构骨架 (按序组织本章, 覆盖全部要点, 可按证据多寡合并小节)",
    ]
    lines.extend(f"{i}. {item}" for i, item in enumerate(ch["structure"], 1))
    lines.extend([
        "",
        "## 事实证据摘要 (fact 层 · 可作事实句引用, 标注 [E:eid])",
        (facts_digest or "").strip() or "(本章未注入事实证据: 相关陈述一律写 “待验证”)",
        "",
        "## 研报观点摘要 (view 层 · 只可转述, 标注 [V:vid], 永不作事实句)",
        (views_digest or "").strip() or "(本章未注入研报观点: 不要凭记忆转述任何机构观点)",
        "",
        "## 输出约定",
        f"- 篇幅 {ch['length_hint']}; 首行为章标题 `## 第{ch['no']}章 {ch['title']}`, 小节用 `###`。",
        "- 事实句带 [E:eid]; 观点句带 [V:vid] 并以 “机构(日期)认为” 引出; 判断句给假设+验证指标。",
        "- 摘要中没有的数字一律写 “待验证”; 只输出本章 Markdown 正文, 不输出其他内容。",
    ])
    if ch["no"] == HYPOTHESES_CHAPTER_NO:
        lines.append(_HYPOTHESES_REQUEST)
    return "\n".join(lines).strip() + "\n"
