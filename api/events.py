"""Shared HTTP SSE framing; no business logic."""
import json
from typing import Any

def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"
