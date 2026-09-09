"""
LLM 客户端封装
- 消除原 infra/openai.py 中 ChatOpenAI 类名与 langchain_openai.ChatOpenAI 的冲突
- api_key / base_url / model 统一从 core.config 读取
- 支持多模型路由: 按"用途" (purpose) 取模型, 配置见 config.yaml llm.models

config.yaml 结构:
    llm:
      enable_thinking: true            # 思考开关全局默认 (深思考=最强推理; 开启时必须配足 max_tokens)
      max_tokens: 16384                # 全局输出预算 (思考与正文共用, 勿设小值)
      models:
        default:   {model: xxx}          # 兜底 (必填)
        agent:     {model: xxx}          # Skill/Agent 推理 (高性能)
        extract:   {model: xxx}          # 研报结构化提取
        vision:    {model: xxx, enable_thinking: false}   # 用途级覆盖思考开关
        embedding: {model: xxx}          # 向量 (预留)
      # 各用途可单独覆盖 api_key/base_url (默认用 .env 的 OPENAI_API_KEY/OPENAI_BASE_URL)

思考开关注入 (client 统一处理, 业务代码不再传 enable_thinking):
    显式 kwargs > 用途覆盖 llm.models.<purpose>.enable_thinking > 全局 llm.enable_thinking > True

用途 -> 场景映射 (代码里按语义取, 不感知具体模型名):
    agent    : FinQuerySkill SQL生成/结果解读, ReportSkill 综合解读/问答, 意图识别 L2
    extract  : ReportSkill 结构化提取 (长文本 JSON 输出)
    vision   : 图片型 PDF 页面识别 (OCR 降级)
    default  : 其余一切
"""
from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Union

from openai import OpenAI

from core.config import get_config

MessageList = List[Dict[str, Any]]

# 已注册的用途 (config llm.models 之外的用途回落 default)
KNOWN_PURPOSES = {"default", "agent", "extract", "analysis", "vision", "nl2sql",
                  "embedding"}


class LLMClient:
    """OpenAI 兼容的 LLM 客户端 (支持按用途路由模型)"""

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
        # 显式 model 参数优先; 其次按用途路由; 最后 default
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

    # ---------- 默认参数 (思考开关 + 输出预算) ----------
    def _default_kwargs(self) -> Dict[str, Any]:
        """
        思考与 token 预算统一注入:
        - enable_thinking: 用途覆盖 > 全局 llm.enable_thinking > 默认开启 (最强思考)
        - max_tokens: 全局 llm.max_tokens (思考与正文共用预算, 必须给足,
          曾因小预算思考耗尽导致 content 为空 — 见 docs/agents/llm_models.md)
        """
        cfg = get_config()
        kw: Dict[str, Any] = {}
        mt = cfg.get("llm.max_tokens")
        if mt:
            kw["max_tokens"] = int(mt)
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

    def invoke(self, input_data: Union[str, MessageList], **kwargs) -> str:
        """同步调用, 返回文本"""
        messages = self._normalize(input_data)
        response = self.client.chat.completions.create(
            model=self.model, messages=messages,
            **{**self._default_kwargs(), **kwargs}
        )
        return response.choices[0].message.content or ""

    def invoke_with_tools(self, messages: MessageList, tools: List[Dict],
                          tool_choice: str = "auto", **kwargs):
        """function-calling 调用: 返回完整 assistant message (content + tool_calls)

        供 Agent 循环使用 (deepseek OpenAI 兼容):
        - message.tool_calls: List[{id, function:{name, arguments}}] 或 None
        - message.content: 无工具调用时的最终回答文本
        """
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, tools=tools,
            tool_choice=tool_choice,
            **{**self._default_kwargs(), **kwargs}
        )
        return response.choices[0].message

    def stream(self, input_data: Union[str, MessageList], **kwargs) -> Iterator[str]:
        """流式调用, 逐块产出文本 (思考模型的 reasoning 块自动跳过, 只出正文)"""
        messages = self._normalize(input_data)
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, stream=True,
            **{**self._default_kwargs(), **kwargs}
        )
        for chunk in response:
            content = chunk.choices[0].delta.content if chunk.choices else None
            if content:
                yield content


def get_llm(**kwargs) -> LLMClient:
    """便捷工厂"""
    return LLMClient(**kwargs)


def get_agent_llm(**kwargs) -> LLMClient:
    """Agent/Skill 推理用途 (高性能模型)"""
    kwargs.setdefault("timeout", 120.0)
    return LLMClient(purpose="agent", **kwargs)


def get_extract_llm(**kwargs) -> LLMClient:
    """结构化提取用途"""
    kwargs.setdefault("timeout", 120.0)
    return LLMClient(purpose="extract", **kwargs)


def get_nl2sql_llm(**kwargs) -> LLMClient:
    """快车道 NL2SQL 用途 (config 关思考+小预算: 只产出一条 SELECT, 秒级)"""
    kwargs.setdefault("timeout", 30.0)
    return LLMClient(purpose="nl2sql", **kwargs)


def get_vision_llm(**kwargs) -> LLMClient:
    """视觉用途 (图片型 PDF OCR)"""
    kwargs.setdefault("timeout", 180.0)
    return LLMClient(purpose="vision", **kwargs)
