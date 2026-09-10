"""
领域技能层 (agent/skills, 智能体域子包)

- fin_query:      财务数据问答 (Text-to-SQL)
- report:         研报解读
- scanned_report: 扫描件 OCR
(beta/alpha 技能在 agent/beta_alpha/skills, 经 registry 懒加载统一注册)
"""
from skills.base import BaseSkill, SkillContext
from skills.registry import SkillRegistry, get_skill_registry

__all__ = [
    "BaseSkill",
    "SkillContext",
    "SkillRegistry",
    "get_skill_registry",
]
