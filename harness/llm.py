"""
LLM client wrapper.
- Eliminates the clash between the old infra/openai.py ChatOpenAI class name and langchain_openai.ChatOpenAI
- api_key / base_url / model are all read from core.config
- Supports multi-model routing: model chosen by "purpose"; configuration in config.yaml llm.models

config.yaml structure:
    llm:
      enable_thinking: true            # global default for the thinking switch (deep thinking = strongest reasoning; when enabled, max_tokens must be generous)
      max_tokens: 16384                # global output budget (shared by thinking and body text; do not set it small)
      models:
        default:   {model: xxx}          # fallback (required)
        agent:     {model: xxx}          # Skill/Agent reasoning (high performance)
        extract:   {model: xxx}          # structured extraction of research reports
        vision:    {model: xxx, enable_thinking: false}   # purpose-level override of the thinking switch
        embedding: {model: xxx}          # vectors (reserved)
      # Each purpose may override api_key/base_url (defaults to .env OPENAI_API_KEY/OPENAI_BASE_URL)

Thinking-switch injection (handled uniformly by the client; business code no longer passes enable_thinking):
    explicit kwargs > purpose override llm.models.<purpose>.enable_thinking > global llm.enable_thinking > True

Purpose -> scenario mapping (code selects by semantics, unaware of concrete model names):
    agent    : FinQuerySkill SQL generation / result interpretation, ReportSkill synthesis / Q&A, intent recognition L2
    extract  : ReportSkill structured extraction (long-text JSON output)
    vision   : image-only PDF page recognition (OCR fallback)
    default  : everything else
"""
from __future__ import annotations

import time
from typing import Any, Dict, Iterator, List, Optional, Union

from openai import OpenAI

from core.config import get_config
from core.metrics import record as metrics_record

MessageList = List[Dict[str, Any]]


class LLMClient:

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 30.0,
        max_retries: int = 1,
        purpose: Optional[str] = None,
    ):
        config = get_config()
        # Explicit model parameter wins; then purpose-based routing; finally default
        if model:
            self.model = model
        else:
            self.model = config.llm_model_for(purpose or "default")
        self.purpose = purpose or "default"
        override = config.llm_model_override(purpose or "default")
        self.client = OpenAI(
            api_key=(override.get("api_key") if override else None)
            or api_key or config.openai_api_key,
            base_url=(override.get("base_url") if override else None)
            or base_url or config.openai_base_url,
            timeout=timeout,
            max_retries=max_retries,
        )

    # ---------- Default kwargs (thinking switch + output budget) ----------
    def _default_kwargs(self) -> Dict[str, Any]:
        """
        Thinking and token budget are injected uniformly here:
        - enable_thinking: purpose override > global llm.enable_thinking > default on (strongest thinking)
        - max_tokens: global llm.max_tokens (shared budget for thinking and body text; must be generous —
          a too-small budget once let thinking exhaust it and yield empty content; see docs/agents/llm_models.md)
        """
        cfg = get_config()
        kw: Dict[str, Any] = {}
        mt = cfg.get("llm.max_tokens")
        if mt:
            # Tolerance: accept thousands separators (config once contained "32,768")
            kw["max_tokens"] = int(str(mt).replace(",", "").strip())
        et = cfg.get(f"llm.models.{self.purpose}.enable_thinking")
        if et is None:
            et = cfg.get("llm.enable_thinking", True)
        kw["extra_body"] = {"enable_thinking": bool(et)}
        return kw

    @staticmethod
    def _normalize(input_data: Union[str, MessageList]) -> MessageList:
        if isinstance(input_data, str):
            return [{"role": "user", "content": input_data}]
        return input_data

    def _tracked(self, stream: bool, **create_kwargs):
        """create() with monitoring instrumentation: latency / success / tokens (core.metrics; exceptions swallowed so business flow is unaffected)."""
        t0 = time.time()
        merged = {**self._default_kwargs(), **create_kwargs}
        try:
            response = self.client.chat.completions.create(
                model=self.model, stream=stream, **merged)
        except Exception as e:
            metrics_record("llm", purpose=self.purpose, ok=False,
                           ms=int((time.time() - t0) * 1000))
            raise
        usage = getattr(response, "usage", None)
        metrics_record("llm", purpose=self.purpose, ok=True,
                       ms=int((time.time() - t0) * 1000),
                       pt=getattr(usage, "prompt_tokens", 0) or 0,
                       ct=getattr(usage, "completion_tokens", 0) or 0)
        return response

    def invoke(self, input_data: Union[str, MessageList], **kwargs) -> str:
        """Synchronous call, returns text."""
        messages = self._normalize(input_data)
        response = self._tracked(stream=False, messages=messages, **kwargs)
        return response.choices[0].message.content or ""

    def invoke_with_tools(self, messages: MessageList, tools: List[Dict],
                          tool_choice: str = "auto", **kwargs):
        """function-calling invocation: returns the full assistant message (content + tool_calls).

        Used by the Agent loop (deepseek OpenAI-compatible):
        - message.tool_calls: List[{id, function:{name, arguments}}] or None
        - message.content: final answer text when no tool was called
        """
        response = self._tracked(stream=False, messages=messages, tools=tools,
                                 tool_choice=tool_choice, **kwargs)
        return response.choices[0].message

    def stream(self, input_data: Union[str, MessageList], **kwargs) -> Iterator[str]:
        """Streaming call, yields text chunks (reasoning blocks of thinking models are skipped automatically; body text only)."""
        messages = self._normalize(input_data)
        response = self._tracked(stream=True, messages=messages, **kwargs)
        for chunk in response:
            content = chunk.choices[0].delta.content if chunk.choices else None
            if content:
                yield content


def get_agent_llm(**kwargs) -> LLMClient:
    """Agent/Skill reasoning purpose (high-performance model)."""
    kwargs.setdefault("timeout", 120.0)
    return LLMClient(purpose="agent", **kwargs)


def get_extract_llm(**kwargs) -> LLMClient:
    """Structured-extraction purpose."""
    kwargs.setdefault("timeout", 120.0)
    return LLMClient(purpose="extract", **kwargs)


def get_nl2sql_llm(**kwargs) -> LLMClient:
    """Fast-lane NL2SQL purpose (config disables thinking + small budget: emits a single SELECT, seconds-level)."""
    kwargs.setdefault("timeout", 30.0)
    return LLMClient(purpose="nl2sql", **kwargs)


def get_vision_llm(**kwargs) -> LLMClient:
    """Vision purpose (image-only PDF OCR)."""
    kwargs.setdefault("timeout", 180.0)
    return LLMClient(purpose="vision", **kwargs)
