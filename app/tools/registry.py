"""工具注册表：统一 schema 生成 + 安全执行（失败兜底）。

可靠性设计要点：工具失败永远返回结构化错误 dict 而不是抛异常中断循环，
LLM 看到错误后可以自己换参数重试、换工具、或转向升级。
"""
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.llm.base import ToolCall


@dataclass
class ToolContext:
    """执行工具时的上下文：配置 + 当前会话状态（execute_action 需要）。

    用 duck typing 引用 SessionState，避免 tools -> agent 的循环依赖。
    """

    settings: Any = None
    state: Any = None


class Tool:
    """工具基类：子类声明 name/description/parameters(JSON schema) 并实现 run()。"""

    name: str = ""
    description: str = ""
    parameters: Dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    def run(self, ctx: ToolContext, **kwargs) -> Dict[str, Any]:
        raise NotImplementedError


class ToolRegistry:
    def __init__(self):
        self._tools: Dict[str, Tool] = {}
        self.chaos: set = set()  # 评测注入：在集合中的工具名会被强制失败

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def schemas(self) -> List[Dict[str, Any]]:
        """转成 OpenAI function calling 格式。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    # 空结果判定：检索工具返回 0 条结果时算 not_found 而非 ok，
    # 避免 LLM 把"没查到"误判为"没有问题"。
    # lookup_user 不在此集合：它的空结果已在 summarize_tool_result 中处理。
    _EMPTY_RESULT_TOOLS = {"search_kb", "search_tickets"}

    def safe_execute(self, tc: ToolCall, ctx: ToolContext) -> Dict[str, Any]:
        """执行工具并兜底所有失败路径，永不抛异常。

        返回结构统一为：
          - 成功: {"status": "ok", "data": ...}
          - 失败: {"status": "error", "error_type": "...", "error": "..."}
        error_type 取值: unknown_tool | service_error | timeout | argument_error | not_found
        """
        tool = self._tools.get(tc.name)
        if tool is None:
            return {"status": "error", "error_type": "unknown_tool",
                    "error": f"未知工具: {tc.name}"}
        if tc.name in self.chaos:
            return {"status": "error", "error_type": "service_error",
                    "error": f"工具 {tc.name} 暂时不可用（评测注入的模拟故障）"}

        timeout = getattr(ctx.settings, "tool_timeout_seconds", 15)
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(tool.run, ctx, **tc.arguments)
                data = fut.result(timeout=timeout)
        except FuturesTimeout:
            return {"status": "error", "error_type": "timeout",
                    "error": f"工具 {tc.name} 执行超时（>{timeout}s）"}
        except TypeError as exc:
            # 参数名/数量不对：附上 schema 提示，引导 LLM 自纠参数
            hint = json.dumps(tool.parameters, ensure_ascii=False)
            return {
                "status": "error",
                "error_type": "argument_error",
                "error": f"参数错误: {exc}。期望的参数 schema: {hint}",
            }
        except Exception as exc:  # noqa: BLE001 —— 兜底所有异常，保证循环不断
            if getattr(ctx.settings, "debug", False):
                traceback.print_exc()
            return {"status": "error", "error_type": "service_error",
                    "error": f"{type(exc).__name__}: {exc}"}

        # 空结果：检索工具返回 0 条 → 显式 not_found
        if tc.name in self._EMPTY_RESULT_TOOLS and self._is_empty(data):
            return {"status": "error", "error_type": "not_found",
                    "error": f"{tc.name} 未返回匹配结果（无该问题的已知信息）"}

        return {"status": "ok", "data": data}

    @staticmethod
    def _is_empty(data: Any) -> bool:
        if not isinstance(data, dict):
            return False
        results = data.get("results") or data.get("users") or []
        return len(results) == 0
