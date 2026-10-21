"""OpenAI 兼容 provider：一个类同时支持 DeepSeek / OpenAI / 任何兼容端点。

为什么不写 DeepseekLLM 和 OpenAILLM 两个类？
DeepSeek API 完全遵循 OpenAI Chat Completions 协议（含 function calling），
两家的差异只有 base_url / model / api_key 三个配置项。
识别"协议兼容性"可以避免一层伪抽象 —— 切换 provider 只需改 .env。
"""
from typing import Any, Dict, List, Optional

from openai import OpenAI

from app.llm.base import BaseLLM, LLMResponse, ToolCall, Usage


class OpenAICompatLLM(BaseLLM):
    def __init__(self, settings):
        # 没配 key 时尽早失败，给出清晰报错而不是等到第一次调用才炸
        if not settings.llm_api_key:
            raise ValueError(
                f"LLM_PROVIDER={settings.llm_provider} 但未配置 LLM_API_KEY，"
                "请在 .env 中填入 API Key（参考 .env.example）"
            )
        self.client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
        self.model = settings.llm_model

    def complete(
        self,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        resp = self.client.chat.completions.create(**kwargs)
        choice = resp.choices[0]
        msg = choice.message

        tool_calls = [
            ToolCall(
                id=t.id,
                name=t.function.name,
                arguments=_parse_json(t.function.arguments or "{}"),
            )
            for t in (msg.tool_calls or [])
        ]
        usage = None
        if resp.usage is not None:
            usage = Usage(
                prompt_tokens=resp.usage.prompt_tokens or 0,
                completion_tokens=resp.usage.completion_tokens or 0,
            )
        return LLMResponse(
            content=msg.content,
            tool_calls=tool_calls,
            finish_reason=choice.finish_reason or "stop",
            usage=usage,
        )


def _parse_json(raw: str) -> dict:
    """宽容解析工具参数 JSON；解析失败返回空 dict，让上层以参数错误反馈给 LLM 自纠。"""
    import json

    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}
