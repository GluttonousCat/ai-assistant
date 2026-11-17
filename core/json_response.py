"""Parse model responses without introducing model or business dependencies."""

import json
import re
from typing import Any


def parse_json_response(text: str, *, fences: str = "leading") -> Any:
    if not text:
        return None
    value = text.strip()
    if fences == "all":
        value = re.sub(r"```(?:json)?|```", "", value).strip()
    elif fences == "leading":
        if value.startswith("```"):
            value = value.strip("`").strip()
            if value.startswith("json"):
                value = value[4:]
    elif fences != "raw":
        raise ValueError("Unsupported JSON fence mode")
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
    return None
