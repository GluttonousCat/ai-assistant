# -*- encoding: utf-8 -*-
"""T05 提示词库的最小 stub — 仅当 skills.research.prompts_chapters / prompts_style
(任务 T05, 其他分支) 尚未合并时, 被 generator._load_prompt_lib 回退加载。

**T05 合并后删除本文件, 并去掉 generator 里的回退分支。**

章节结构摘自 docs/research/company_industry_guide.md「公司研究的完整章节结构」;
字段契约 (no/title/question/wants/structure/length_hint) 与
chapter_prompt(ch, facts_digest, views_digest) 签名见
docs/research/tasks/T05_十章模板与提示词库.md。
本文件是纯数据与字符串拼装, 不 import 任何重模块, 不写死任何公司数据;
wants 取值与 T05 落库模板的 EVIDENCE_CATEGORIES 闭集对齐
(profile/mainbiz/financials/annual/views); 假设章序 HYPOTHESES_CHAPTER_NO=8
(情景分析章 — T06 任务书"第10章"系章序笔误, 以 T05 勘正为准)。
"""
from __future__ import annotations

from typing import Any, Dict, List

HYPOTHESES_CHAPTER_NO = 8

CHAPTERS: List[Dict[str, Any]] = [
    {"no": 1, "title": "公司定位与研究结论摘要",
     "question": "这家公司到底是什么? 当前最重要的经营变量是什么?",
     "wants": ["profile", "mainbiz", "views"],
     "structure": ["一页公司画像 (主营/产业链位置/客户结构)",
                    "核心经营驱动因素",
                    "3~5个待验证的关键研究问题"],
     "length_hint": "600-900字"},
    {"no": 2, "title": "商业模式与业务拆解",
     "question": "公司通过什么方式创造收入和利润?",
     "wants": ["mainbiz", "annual", "views"],
     "structure": ["主要产品及服务, 客户是谁、采购动机",
                    "收入按产品/地区/业务分部构成",
                    "交易模式、定价方式、订单周期",
                    "成本构成及主要经营杠杆"],
     "length_hint": "800-1200字"},
    {"no": 3, "title": "产业链位置与竞争格局",
     "question": "公司在价值链上具有什么位置? 客户为什么选择它?",
     "wants": ["profile", "annual", "views"],
     "structure": ["上游原材料/设备/能源及供应商",
                    "公司制造或研发环节; 下游客户与终端需求",
                    "竞争对手与替代方案对照",
                    "技术/规模/客户认证/供应链等竞争机制分析"],
     "length_hint": "800-1200字"},
    {"no": 4, "title": "历史经营与财务质量",
     "question": "公司过去的增长和利润来自哪里? 经营质量如何?",
     "wants": ["financials", "annual"],
     "structure": ["收入/毛利/营业利润/净利润趋势",
                    "现金流与利润的匹配关系",
                    "资产负债结构、应收/存货/合同负债",
                    "ROE/ROIC/资产周转率; 研发投入与资本开支"],
     "length_hint": "1000-1500字"},
    {"no": 5, "title": "经营驱动因子与盈利机制",
     "question": "哪些变量真正决定公司的收入和利润?",
     "wants": ["annual", "financials"],
     "structure": ["价格、销量、产能、利用率四因子",
                    "产品结构与客户结构变化",
                    "原材料/设备/人工/折旧成本传导",
                    "资本开支到产能释放的传导; 规模效应"],
     "length_hint": "800-1200字"},
    {"no": 6, "title": "技术、研发与竞争优势",
     "question": "公司的竞争优势是否具有持续性?",
     "wants": ["annual", "financials", "views"],
     "structure": ["技术路线与产品能力",
                    "工艺/良率/性能/成本竞争指标",
                    "研发投入与技术成果转化",
                    "客户认证、转换成本、规模效应等壁垒证据"],
     "length_hint": "800-1200字"},
    {"no": 7, "title": "管理层、治理与资本配置",
     "question": "公司如何进行资源配置和战略执行?",
     "wants": ["annual", "profile", "views"],
     "structure": ["股权结构、关联交易、治理结构",
                    "资本开支/并购/融资/分红",
                    "战略目标与执行进度; 重大项目追踪",
                    "审计意见、内控与治理风险"],
     "length_hint": "800-1200字"},
    {"no": 8, "title": "未来增长与情景分析",
     "question": "未来的经营变化可能通过哪些路径发生?",
     "wants": ["views", "annual", "financials"],
     "structure": ["产能扩张、价格、产品结构路径",
                    "新客户/新产品/新市场",
                    "基准/上行/下行情景",
                    "每个情景的可验证指标"],
     "length_hint": "800-1200字"},
    {"no": 9, "title": "风险与反证分析",
     "question": "什么情况会使当前研究假设失效?",
     "wants": ["annual", "views"],
     "structure": ["行业需求下滑、价格竞争",
                    "产能投放不及预期或利用率不足",
                    "技术路线变化、客户集中度、供应链与政策",
                    "财务数据异常与披露口径变化; 预警条件"],
     "length_hint": "600-1000字"},
    {"no": 10, "title": "估值与持续跟踪",
     "question": "如何把经营假设映射到估值分析, 并持续验证?",
     "wants": ["views", "financials", "profile"],
     "structure": ["估值方法与适用条件",
                    "关键估值假设及敏感性; 同业口径差异",
                    "后续财报/公告/经营指标跟踪清单",
                    "研究结论更新记录"],
     "length_hint": "600-1000字"},
]

STYLE_PROMPT = """你是一名严谨的行业公司研究分析师, 正在为公司深度研究报告撰写其中一章。
写作铁律 (任何一章都必须遵守):

一、三层分离 (最高纪律)
1. 事实句: 只陈述「事实证据摘要」中给出的内容, 数据一律照抄 (含单位与期间),
   句末标注行内引用 [E:eid]。
2. 研报观点句: 必须以「XX机构(日期)认为/预计…」的转述句式书写, 句末标注 [V:vid];
   研报观点永远不能当作事实使用, 不得与事实句混写。
3. 证据摘要之外的数字一律写「待验证」, 严禁凭模型记忆编造; 表达自身推断时用
   「我们判断/本报告假设」并给出可验证的口径。

二、引用格式
- 行内引用只允许 [E:eid] 与 [V:vid] 两种写法, 编号必须严格照抄证据摘要中出现的
  eid/vid, 禁止自造、缩写或改写。
- 渲染层会把行内引用转为脚注, 编号准确是底线。

三、文风
- 机制解释优先于信息罗列: 每个重要结论回答「为什么/通过什么机制/正在变化什么/
  如何验证」。
- 禁营销腔与形容词堆砌; 金额必带单位 (亿元/万元), 比率带 %。
- 不做投资建议, 不出现「买入/卖出/目标价可期」式表述; 章末以「观察点」收束
  (1-3 条, 指向可跟踪的指标或事件)。
- 全文简体中文。

四、输出
- 直接输出本章 Markdown 正文 (可用 ### 小节与表格), 不输出寒暄、解释或与正文无关的说明。"""


# 假设章追加指令 (普通字符串, 不走 .format — JSON 花括号原样保留)
_HYPOTHESES_REQUEST = """
## 追加输出 · 研究假设 JSON
在正文之后追加一个 ```json 围栏代码块, 输出本章识别的研究假设数组 (供下游结构化回填):
[{"statement": "假设陈述(一句话)", "indicators": ["验证指标(含数据来源提示)"], "related_eids": ["支撑/相关证据 id"]}]
规则: 每条假设必须可验证 (indicators 至少 1 条非空); related_eids 只用上文摘要中出现过的 id。"""


def chapter_prompt(ch: Dict[str, Any], facts_digest: str = "", views_digest: str = "") -> str:
    """单章 prompt 拼装 (只做字符串拼装, 不调 LLM): 章问题 + 结构骨架 + 证据注入位 + 输出约定。"""
    skeleton = "\n".join(f"- {s}" for s in ch.get("structure", []))
    prompt = f"""# 第{ch['no']}章 · {ch['title']}

本章研究问题: {ch['question']}

内容骨架 (按此组织小节, 可按证据取舍微调):
{skeleton or "- (无预置骨架, 按证据自行组织)"}

篇幅要求: {ch.get('length_hint', '800-1200字')}

## 事实证据摘要 (只允许引用其中出现过的 eid)
{facts_digest or "(本章暂无事实证据, 相关论述须写明「待验证」)"}

## 研报观点摘要 (只允许引用其中出现过的 vid; 转述须带机构与日期)
{views_digest or "(本章暂无研报观点)"}"""
    if int(ch["no"]) == HYPOTHESES_CHAPTER_NO:
        prompt += _HYPOTHESES_REQUEST
    return prompt
