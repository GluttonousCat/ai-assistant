"""
Company Skill - 上市公司基本面分析
分析公司业务、财务、估值、风险等维度
"""
from skills.base import BaseSkill, SkillContext


class CompanySkill(BaseSkill):
    name = "company"
    description = "上市公司基本面分析: 分析公司业务模式、财务状况、估值水平、投资风险"
    intent_keywords = ["分析", "基本面", "财务", "估值", "怎么样"]

    def run(self, context: SkillContext) -> SkillContext:
        # TODO(阶段五): 公司分析实现
        # 1. 获取公司基本信息 (行业、市值、主营业务)
        # 2. 获取财务数据 (营收、利润、ROE等)
        # 3. LLM 多维度分析
        context.result = "Company Skill 待实现 (阶段五)"
        context.log("Company: 需要实现基本面分析流程")
        return context
