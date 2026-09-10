"""
Skill 基类 (领域方法论层)

Skill 是 Agent 可调用的领域能力单元:
- 每个 Skill 有名称、描述、意图关键词
- Skill 内部编排 Tool + LLM, 完成一个完整的分析任务
- 与 Tool 的区别: Tool 是原子能力, Skill 是组合多个 Tool 的领域方法论
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SkillContext:
    """Skill 执行上下文"""
    user_input: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    result: Any = None
    error: Optional[str] = None
    logs: List[str] = field(default_factory=list)

    def log(self, message: str):
        self.logs.append(message)


class BaseSkill(ABC):
    """Skill 基类"""

    name: str = ""
    description: str = ""
    intent_keywords: List[str] = []

    def match_intent(self, user_input: str) -> bool:
        """判断用户输入是否匹配本 Skill"""
        text = user_input.lower()
        return any(kw in text for kw in self.intent_keywords)

    @abstractmethod
    def run(self, context: SkillContext) -> SkillContext:
        """执行 Skill 主逻辑"""
        raise NotImplementedError

    def __call__(self, context: SkillContext) -> SkillContext:
        return self.run(context)
