"""
ChatBI Skill - 上市公司数据问答
通过 Text-to-SQL 查询 MySQL 中的股票数据, 用自然语言回答用户问题
"""
from skills.base import BaseSkill, SkillContext


class ChatBISkill(BaseSkill):
    name = "chatbi"
    description = "上市公司数据问答: 根据自然语言生成SQL查询股票数据"
    intent_keywords = ["查询", "查一下", "数据", "多少", "排名", "对比"]

    def run(self, context: SkillContext) -> SkillContext:
        # TODO(阶段三): Text-to-SQL 实现
        # 1. 理解用户问题 → 提取查询意图
        # 2. 生成 SQL → 校验安全性
        # 3. 执行 SQL → 获取结果
        # 4. LLM 解读结果 → 自然语言回答
        context.result = "ChatBI Skill 待实现 (阶段三)"
        context.log("ChatBI: 需要实现 Text-to-SQL 流程")
        return context
