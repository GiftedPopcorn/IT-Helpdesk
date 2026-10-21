"""get_system_status 工具：查询服务健康、活跃故障、近期变更。"""
import json
import os
from typing import Any, Dict

from app.tools.registry import Tool, ToolContext


class SystemStatusTool(Tool):
    name = "get_system_status"
    description = (
        "查询系统状态：服务健康度、活跃故障（INC）、近期变更（CHG）。"
        "诊断连接类/变慢类问题时先查这里，变更日志常直接指向根因。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "service": {
                "type": "string",
                "description": "服务名（如 vpn-gateway / email / sso）。留空返回全部概览",
            }
        },
        "required": [],
    }

    def __init__(self, status_path: str):
        self._path = status_path

    def _load(self) -> Dict[str, Any]:
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f)

    def run(self, ctx: ToolContext, service: str = "") -> Dict[str, Any]:
        data = self._load()
        if not service:
            return {"source": "system_status", **data}

        key = service.lower()
        services = [s for s in data.get("services", []) if key in s.get("name", "").lower()]
        incidents = [
            i
            for i in data.get("active_incidents", [])
            if any(key in s.lower() for s in i.get("services", []))
        ]
        changes = [
            c for c in data.get("recent_changes", []) if key in c.get("service", "").lower()
        ]
        return {
            "source": "system_status",
            "service": service,
            "services": services,
            "active_incidents": incidents,
            "recent_changes": changes,
        }
