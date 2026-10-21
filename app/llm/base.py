"""BaseLLM 抽象接口与响应数据结构。

所有 provider（DeepSeek / OpenAI / Mock）都实现同一个 complete() 签名，
Agent 循环只依赖这个接口 —— 这是"接口都留下，到时候再选"的落点。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ToolCall:
    """一次工具调用请求（参数已解析为 dict，不是原始 JSON 字符串）。"""

    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class Usage:
    """单次调用的 token 用量（用于成本/延迟观测）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class LLMResponse:
    """complete() 的标准化返回。"""

    content: Optional[str] = None  # 文本回复；最终答案时是 JSON 字符串
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"  # "tool_calls" | "stop"
    usage: Optional[Usage] = None


class BaseLLM(ABC):
    """统一 LLM 接口。

    messages 直接使用 OpenAI 消息格式（含 role=tool 的工具结果消息），
    因为 function calling 的多轮协议要求 assistant 的 tool_calls 原样回传。
    """

    @abstractmethod
    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        ...
