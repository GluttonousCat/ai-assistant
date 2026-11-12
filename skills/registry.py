"""
Skill registry.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from skills.base import BaseSkill

from core.logger import get_logger

logger = get_logger(__name__)


class SkillRegistry:
    """Skill registry (singleton)."""

    _instance: Optional["SkillRegistry"] = None
    _skills: Dict[str, BaseSkill]

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills = {}
        return cls._instance

    def register(self, skill: BaseSkill):
        """Register a skill."""
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[BaseSkill]:
        """Get a skill by name."""
        return self._skills.get(name)

    def list_all(self) -> List[BaseSkill]:
        """List all registered skills."""
        return list(self._skills.values())

    def match_by_intent(self, user_input: str) -> Optional[BaseSkill]:
        """Match the first skill whose intent keywords fit the user input."""
        for skill in self._skills.values():
            if skill.match_intent(user_input):
                return skill
        return None

    def describe_all(self) -> str:
        """Build a description line per skill (consumed by LLM routing)."""
        lines = []
        for skill in self._skills.values():
            lines.append(f"- {skill.name}: {skill.description}")
        return "\n".join(lines)


def get_skill_registry() -> SkillRegistry:
    return SkillRegistry()


def register_builtin_skills() -> SkillRegistry:
    """Register the project's built-in skills.

    Registered incrementally by milestone: M1 financial query, M3 anomaly
    detection, M4 report analysis.
    """
    registry = get_skill_registry()

    # M1: financial query (Text-to-SQL)
    try:
        from skills.fin_query.skill import FinQuerySkill
        registry.register(FinQuerySkill())
    except Exception as e:
        logger.warning(f"FinQuerySkill 注册失败: {e}")

    # M4: report analysis (zsxq research-report interpretation)
    try:
        from skills.report.skill import ReportSkill
        registry.register(ReportSkill())
    except Exception as e:
        logger.warning(f"ReportSkill 注册失败: {e}")

    # M4+: scanned-report analysis (image-only PDF via vision OCR)
    try:
        from skills.scanned_report.skill import ScannedReportSkill
        registry.register(ScannedReportSkill())
    except Exception as e:
        logger.warning(f"ScannedReportSkill 注册失败: {e}")

    # Industry-chain Beta mining (three-source mapping + segment index; self-governing module beta_alpha/)
    try:
        from skills.beta_alpha.skills.beta import BetaSkill
        registry.register(BetaSkill())
    except Exception as e:
        logger.warning(f"BetaSkill 注册失败: {e}")

    # Stock expectation gap (four quadrants; self-governing module beta_alpha/)
    try:
        from skills.beta_alpha.skills.alpha import AlphaSkill
        registry.register(AlphaSkill())
    except Exception as e:
        logger.warning(f"AlphaSkill 注册失败: {e}")

    return registry


def get_ready_skills() -> SkillRegistry:
    """Get the registry with all built-in skills registered."""
    return register_builtin_skills()
