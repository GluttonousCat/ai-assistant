"""
Report Skill - 研报分析
解读券商研报, 提取关键信息和投资观点
"""
from skills.base import BaseSkill, SkillContext


class ReportSkill(BaseSkill):
    name = "report"
    description = "研报分析: 解读券商研报, 提取核心观点、盈利预测、评级变化"
    intent_keywords = ["研报", "报告", "解读", "观点"]

    def run(self, context: SkillContext) -> SkillContext:
        # TODO(阶段五): 研报分析实现
        # 1. 获取研报文本 (可从知识星球下载或本地上传)
        # 2. LLM 提取关键信息
        # 3. 结构化输出
        context.result = "Report Skill 待实现 (阶段五)"
        context.log("Report: 需要实现研报解读流程")
        return context
