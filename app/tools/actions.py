"""execute_action 工具：白名单动作执行器（硬约束层）。

这是整个系统的"最后一道闸门"：
- 白名单之外的动作一律 denied
- 即使白名单内，也要过 PolicyEngine 二次校验（身份核验、角色限制）
- LLM 只能"请求"动作，能不能执行由这里的确定性代码说了算
"""
import secrets
from typing import Any, Dict

from app.tools.policy import PolicyEngine
from app.tools.registry import Tool, ToolContext

# 代码级白名单：与 policy.json 双重校验（defense in depth）
WHITELIST = {"reset_password", "unlock_account", "grant_standard_software"}


class ExecuteActionTool(Tool):
    name = "execute_action"
    description = (
        "执行运维动作（改密码/解锁账号/授予标准软件授权）。"
        "仅限策略允许且身份已核验的情形；被拒绝时会返回策略条款，此时应走升级流程。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": sorted(WHITELIST),
                "description": "动作名",
            },
            "params": {
                "type": "object",
                "description": "动作参数，如 {employee_id: 'E1001', software: 'grafana_readonly'}",
            },
        },
        "required": ["action"],
    }

    def __init__(self, engine: PolicyEngine):
        self._engine = engine

    def run(self, ctx: ToolContext, action: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
        params = params or {}
        state = ctx.state
        user = getattr(state, "user", None) if state is not None else None
        verified = bool(getattr(getattr(state, "issue", None), "verified_identity", False))

        # 第一道：代码级白名单
        if action not in WHITELIST:
            return {
                "source": "action",
                "status": "denied",
                "policy_clause": f"{action} 不在 agent 动作白名单中，必须转人工处理",
            }
        # 第二道：策略引擎（身份核验 + 角色条件）
        allowed, clause = self._engine.allowed(action, user, verified)
        if not allowed:
            return {"source": "action", "status": "denied", "policy_clause": clause}

        # 执行（mock 实现）
        if action == "reset_password":
            target = params.get("employee_id") or (user or {}).get("employee_id", "")
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "result": {
                    "employee_id": target,
                    "temporary_password": f"Temp-{secrets.randbelow(9000) + 1000}",
                    "expires_in_minutes": 30,
                    "note": "用户下次登录需修改密码",
                },
            }
        if action == "unlock_account":
            target = params.get("employee_id") or (user or {}).get("employee_id", "")
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "result": {"employee_id": target, "account_status": "active"},
            }
        if action == "grant_standard_software":
            software = params.get("software", "")
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "result": {
                    "granted": software,
                    "employee_id": (user or {}).get("employee_id", ""),
                    "sync_minutes": 5,
                },
            }
        return {"source": "action", "status": "denied", "policy_clause": f"unhandled:{action}"}
