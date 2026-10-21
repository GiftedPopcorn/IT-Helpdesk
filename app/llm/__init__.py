"""LLM 适配层：统一 BaseLLM 接口，provider 可切换。

设计要点（面试讲点）：
- DeepSeek 完全兼容 OpenAI 协议，因此只写一个 OpenAICompatLLM，
  provider 差异全部收敛为配置项（base_url / model / api_key），不写两个类
- 消息直接用 OpenAI 格式的 dict 而非自定义 pydantic 模型：
  assistant 的 tool_calls 消息必须原样回传给 API 续写对话，dict 透传最不易错
"""
from app.llm.base import BaseLLM, LLMResponse, ToolCall, Usage
from app.llm.mock import MockLLM
from app.llm.openai_compat import OpenAICompatLLM


def build_llm(settings) -> BaseLLM:
    """按配置构建 LLM 实例（工厂函数，Agent 层只依赖 BaseLLM 接口）。"""
    if settings.llm_provider == "mock":
        return MockLLM()
    if settings.llm_provider in ("deepseek", "openai"):
        return OpenAICompatLLM(settings)
    raise ValueError(f"未知 LLM provider: {settings.llm_provider}")


__all__ = ["BaseLLM", "LLMResponse", "ToolCall", "Usage", "MockLLM", "OpenAICompatLLM", "build_llm"]
