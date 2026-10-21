"""工具层：注册全部 6 个工具并提供统一构建入口。"""
from app.tools.policy import CheckPolicyTool, PolicyEngine
from app.tools.registry import Tool, ToolContext, ToolRegistry


def build_default_registry(settings) -> ToolRegistry:
    """按配置装配默认工具集（数据源路径全部来自 settings.data_dir）。"""
    import os

    from app.tools.actions import ExecuteActionTool
    from app.tools.directory import LookupUserTool
    from app.tools.kb import KBSearchTool
    from app.tools.status import SystemStatusTool
    from app.tools.tickets import SearchTicketsTool

    data_dir = settings.data_dir
    registry = ToolRegistry()
    engine = PolicyEngine(os.path.join(data_dir, "policy.json"))

    registry.register(KBSearchTool(os.path.join(data_dir, "kb")))
    registry.register(SystemStatusTool(os.path.join(data_dir, "system_status.json")))
    registry.register(LookupUserTool(os.path.join(data_dir, "user_directory.json")))
    registry.register(SearchTicketsTool(os.path.join(data_dir, "ticket_history.json")))
    registry.register(CheckPolicyTool(engine))
    registry.register(ExecuteActionTool(engine))
    return registry


__all__ = ["Tool", "ToolRegistry", "ToolContext", "PolicyEngine", "build_default_registry"]
