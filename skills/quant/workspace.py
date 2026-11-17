"""Strategy authoring and natural-language compilation, without executing code."""

import json

from core.json_response import parse_json_response
from harness.llm import get_agent_llm
from skills.quant.workspace_models import StrategyDraft
from skills.quant.prompts import STRATEGY_DRAFT_PROMPT
from tools.market import quant_workspace as store


def catalog(owner):
    return {
        "drafts": store.list_drafts(owner),
        "tasks": store.list_tasks(owner),
        "defaults": StrategyDraft().model_dump(),
    }


def generate(request):
    schema = StrategyDraft.model_json_schema()
    messages = [
        {"role": "system", "content": STRATEGY_DRAFT_PROMPT},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "schema": schema,
                    "base": request.base.model_dump(),
                    "user_request": request.text,
                },
                ensure_ascii=False,
            ),
        },
    ]
    value = parse_json_response(get_agent_llm(timeout=90).invoke(messages))
    if not isinstance(value, dict) or set(value) != {
        "draft",
        "assumptions",
        "unsupported",
    }:
        raise ValueError("模型没有返回有效策略草稿，请调整描述后重试")
    for key in ("assumptions", "unsupported"):
        if (
            not isinstance(value[key], list)
            or len(value[key]) > 30
            or any(not isinstance(item, str) or len(item) > 2000 for item in value[key])
        ):
            raise ValueError("模型说明格式不合法，请重试")
    draft = StrategyDraft.model_validate(
        {
            **value["draft"],
            "interpretation_notes": value["assumptions"],
            "unresolved_requirements": value["unsupported"],
        }
    )
    return {**value, "draft": draft.model_dump()}
