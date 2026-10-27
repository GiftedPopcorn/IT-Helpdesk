"""execute_action 工具：白名单动作执行器（硬约束层）。

这是整个系统的"最后一道闸门"：
- 白名单之外的动作一律 denied
- 即使白名单内，也要过 PolicyEngine 二次校验（身份核验、角色限制）
- 自助操作只允许操作已核验身份本人（目标绑定）
- 标准软件授权采用明确白名单，拒绝生产库/管理员等敏感资源
- LLM 只能"请求"动作，能不能执行由这里的确定性代码说了算
"""
import secrets
from typing import Any, Dict

from app.tools.policy import PolicyEngine
from app.tools.registry import Tool, ToolContext

# 代码级白名单：与 policy.json 双重校验（defense in depth）
WHITELIST = {"reset_password", "unlock_account", "grant_standard_software"}

# 标准软件授权白名单（岗位标配软件，不含任何生产/管理员级资源）
STANDARD_SOFTWARE_WHITELIST = {
    "grafana_readonly", "jira", "confluence_readonly",
    "vpn", "email", "sap", "office365", "zoom",
}

# 敏感资源关键词（出现在 software 字符串中即拒绝）
FORBIDDEN_SOFTWARE_KEYWORDS = {
    "production", "prod", "admin", "administrator", "root",
    "snowflake_prod", "database_admin", "sysadmin", "sudo",
}


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
        # Step 4：可信身份必须是"仍有效的核验"，而不仅看 verified_identity 布尔值。
        # 若用户本轮撤回了部门声明或更换了工号，has_valid_identity_verification 返回 False，
        # 动作必须被拒绝（即使之前核验过一次）。
        verified = bool(state is not None and state.has_valid_identity_verification())
        user = state.verified_subject if verified else None

        # 第一道：代码级白名单
        if action not in WHITELIST:
            return {
                "source": "action",
                "status": "denied",
                "policy_clause": f"{action} 不在 agent 动作白名单中，必须转人工处理",
            }

        # 目标绑定：自助操作只允许操作已核验身份本人
        # params.employee_id 缺省时默认指向已核验用户；显式指定他人一律拒绝
        verified_eid = (user or {}).get("employee_id", "") if verified else ""
        target_eid = params.get("employee_id") or verified_eid
        if verified and target_eid and target_eid != verified_eid:
            return {
                "source": "action",
                "status": "denied",
                "policy_clause": (
                    f"{action}:target_mismatch —— 已核验身份为 {verified_eid}，"
                    f"不能代操作 {target_eid}；为他人操作必须转人工"
                ),
            }

        # 第二道：策略引擎（身份核验 + 角色条件，按实际目标角色判定）
        allowed, clause = self._engine.allowed(action, user, verified)
        if not allowed:
            return {"source": "action", "status": "denied", "policy_clause": clause}

        # 第三道：资源白名单（仅 grant_standard_software 需要）
        if action == "grant_standard_software":
            software = (params.get("software") or "").strip()
            if not software:
                return {"source": "action", "status": "denied",
                        "policy_clause": "grant_standard_software: 缺少 software 参数"}
            sw_lower = software.lower()
            if any(kw in sw_lower for kw in FORBIDDEN_SOFTWARE_KEYWORDS):
                return {
                    "source": "action",
                    "status": "denied",
                    "policy_clause": (
                        f"grant_standard_software: '{software}' 含敏感资源关键词，"
                        "生产库/管理员权限必须转人工审批"
                    ),
                }
            if sw_lower not in {s.lower() for s in STANDARD_SOFTWARE_WHITELIST}:
                return {
                    "source": "action",
                    "status": "denied",
                    "policy_clause": (
                        f"grant_standard_software: '{software}' 不在标准软件白名单中，"
                        "非标配软件授权必须转人工审批"
                    ),
                }

        # 执行（mock 实现）
        if action == "reset_password":
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "target": verified_eid,
                "result": {
                    "employee_id": verified_eid,
                    "temporary_password": f"Temp-{secrets.randbelow(9000) + 1000}",
                    "expires_in_minutes": 30,
                    "note": "用户下次登录需修改密码",
                },
            }
        if action == "unlock_account":
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "target": verified_eid,
                "result": {"employee_id": verified_eid, "account_status": "active"},
            }
        if action == "grant_standard_software":
            return {
                "source": "action",
                "status": "ok",
                "action": action,
                "target": verified_eid,
                "result": {
                    "granted": software,
                    "employee_id": verified_eid,
                    "sync_minutes": 5,
                },
            }
        return {"source": "action", "status": "denied", "policy_clause": f"unhandled:{action}"}
