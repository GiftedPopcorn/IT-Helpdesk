"""lookup_user 工具：查询员工目录（工号 / 邮箱 / 姓名）。"""
import json
import os
from typing import Any, Dict, List

from app.tools.registry import Tool, ToolContext

_PUBLIC_FIELDS = [
    "employee_id", "name", "email", "department", "role", "location",
    "devices", "entitlements", "account_status", "sponsor",
]


class LookupUserTool(Tool):
    name = "lookup_user"
    description = (
        "查询员工目录：部门、角色、设备、已有权限、账号状态。"
        "支持工号（E1001）、邮箱或姓名。用于身份核验与权限判断。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "工号 / 邮箱 / 姓名"}
        },
        "required": ["query"],
    }

    def __init__(self, directory_path: str):
        self._path = directory_path

    def _load(self) -> List[Dict[str, Any]]:
        with open(self._path, "r", encoding="utf-8") as f:
            return json.load(f).get("users", [])

    def run(self, ctx: ToolContext, query: str) -> Dict[str, Any]:
        users = self._load()
        q = query.strip().lower()
        matches: List[Dict[str, Any]] = []
        for u in users:
            if (
                u.get("employee_id", "").lower() == q
                or u.get("email", "").lower() == q
                or u.get("name", "").lower() == q
            ):
                matches.append(u)
        if not matches:  # 姓名/工号前缀宽松匹配
            matches = [
                u
                for u in users
                if q in u.get("name", "").lower() or q in u.get("employee_id", "").lower()
            ]
        return {
            "source": "user_directory",
            "query": query,
            "match_count": len(matches),
            "users": [{k: u.get(k) for k in _PUBLIC_FIELDS} for u in matches[:3]],
        }
