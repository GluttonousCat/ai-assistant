"""
LLM 客户端封装
- 消除原 infra/openai.py 中 ChatOpenAI 类名与 langchain_openai.ChatOpenAI 的冲突
- api_key / base_url / model 统一从 core.config 读取
"""
from __future__ import annotations

from typing import Dict, Iterator, List, Optional, Union

from openai import OpenAI

from core.config import get_config

MessageList = List[Dict[str, str]]


class LLMClient:
    """OpenAI 兼容的 LLM 客户端"""

    def __init__(
        self,
        model: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
    ):
        config = get_config()
        self.model = model or config.llm_model
        self.client = OpenAI(
            api_key=api_key or config.openai_api_key,
            base_url=base_url or config.openai_base_url,
        )

    @staticmethod
    def _normalize(input_data: Union[str, MessageList]) -> MessageList:
        if isinstance(input_data, str):
            return [{"role": "user", "content": input_data}]
        return input_data

    def invoke(self, input_data: Union[str, MessageList], **kwargs) -> str:
        """同步调用, 返回文本"""
        messages = self._normalize(input_data)
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, **kwargs
        )
        return response.choices[0].message.content or ""

    def stream(self, input_data: Union[str, MessageList], **kwargs) -> Iterator[str]:
        """流式调用, 逐块产出文本"""
        messages = self._normalize(input_data)
        response = self.client.chat.completions.create(
            model=self.model, messages=messages, stream=True, **kwargs
        )
        for chunk in response:
            content = chunk.choices[0].delta.content
            if content:
                yield content


def get_llm(**kwargs) -> LLMClient:
    """便捷工厂"""
    return LLMClient(**kwargs)
