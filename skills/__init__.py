"""
领域技能层 (Skill Layer)

四大领域 Skill:
- chatbi:  上市公司数据问答 (Text-to-SQL)
- company: 公司基本面分析
- report:  研报解读
- kline:   K线技术分析
"""
from skills.base import BaseSkill, SkillContext
from skills.registry import SkillRegistry, get_skill_registry

__all__ = [
    "BaseSkill",
    "SkillContext",
    "SkillRegistry",
    "get_skill_registry",
]
