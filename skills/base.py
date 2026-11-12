"""
Skill base classes (domain-methodology layer).

A Skill is a domain capability unit the Agent can invoke:
- each Skill has a name, a description, and intent keywords
- a Skill orchestrates Tools + LLM internally to complete a full analysis task
- difference from a Tool: a Tool is an atomic capability, a Skill is a domain
  methodology composed of multiple Tools
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillContext:
    """Skill execution context."""
    user_input: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    def log(self, message: str):
        self.logs.append(message)


class BaseSkill(ABC):
    """Skill base class."""

    name: str = ""
    description: str = ""
    intent_keywords: List[str] = []

    def match_intent(self, user_input: str) -> bool:
        """Return whether the user input matches this skill."""
        text = user_input.lower()
        return any(kw in text for kw in self.intent_keywords)

    @abstractmethod
    def run(self, context: SkillContext) -> SkillContext:
        """Run the skill's main logic."""
        raise NotImplementedError

    def __call__(self, context: SkillContext) -> SkillContext:
        return self.run(context)
