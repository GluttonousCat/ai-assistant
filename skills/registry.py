"""
Skill 注册表
"""
from __future__ import annotations

from typing import Dict, List, Optional

from skills.base import BaseSkill, SkillContext

from core.logger import get_logger

logger = get_logger(__name__)


class SkillRegistry:
    """Skill 注册表"""

    _instance: Optional["SkillRegistry"] = None
    _skills: Dict[str, BaseSkill]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills = {}
        return cls._instance

    def register(self, skill: BaseSkill):
        """注册 Skill"""
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[BaseSkill]:
        """按名称获取 Skill"""
        return self._skills.get(name)

    def list_all(self) -> List[BaseSkill]:
        """列出所有 Skill"""
        return list(self._skills.values())

    def match_by_intent(self, user_input: str) -> Optional[BaseSkill]:
        """根据用户输入匹配最合适的 Skill"""
        for skill in self._skills.values():
            if skill.match_intent(user_input):
                return skill
        return None

    def describe_all(self) -> str:
        """生成所有 Skill 的描述文本 (供 LLM 路由使用)"""
        lines = []
        for skill in self._skills.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)


def get_skill_registry() -> SkillRegistry:
    return SkillRegistry()


def register_builtin_skills() -> SkillRegistry:
    """注册项目内置 Skill
    按里程碑增量注册: M1 财务查询, M3 异常检测, M4 研报校验
    """
    registry = get_skill_registry()

    # M1: 财务查询 (Text-to-SQL)
    try:
        from skills.fin_query.skill import FinQuerySkill
        registry.register(FinQuerySkill())
    except Exception as e:
        logger.warning(f"FinQuerySkill 注册失败: {e}")

    # M4: 研报分析 (知识星球研报解读)
    try:
        from skills.report.skill import ReportSkill
        registry.register(ReportSkill())
    except Exception as e:
        logger.warning(f"ReportSkill 注册失败: {e}")

    # M4+: 扫描件研报分析 (图片型 PDF 视觉识别)
    try:
        from skills.scanned_report.skill import ScannedReportSkill
        registry.register(ScannedReportSkill())
    except Exception as e:
        logger.warning(f"ScannedReportSkill 注册失败: {e}")

    # 产业链Beta挖掘 (三源映射+环节指数; 自治模块 beta_alpha/)
    try:
        from beta_alpha.skills.beta import BetaSkill
        registry.register(BetaSkill())
    except Exception as e:
        logger.warning(f"BetaSkill 注册失败: {e}")

    # 个股预期差 (四象限; 自治模块 beta_alpha/)
    try:
        from beta_alpha.skills.alpha import AlphaSkill
        registry.register(AlphaSkill())
    except Exception as e:
        logger.warning(f"AlphaSkill 注册失败: {e}")

    return registry


def get_ready_skills() -> SkillRegistry:
    """获取已注册的 Skill 注册表 (含内置)"""
    return register_builtin_skills()
