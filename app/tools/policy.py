"""策略引擎 + check_policy 工具。

双层防线设计（面试核心讲点）：
- check_policy 是"软引导"：LLM 能查边界，查了少走弯路
- execute_action 内部强制执行 policy 是"硬约束"：LLM 忘了查也拦得住
- 条件解释器只认已知条件字符串，未知条件一律 fail-closed（视为不允许）
"""
import json
from typing import Any, Dict, Optional, Tuple

from app.tools.registry import Tool, ToolContext


class PolicyEngine:
    """加载 policy.json 并判定"某动作在当前用户上下文下是否允许 agent 执行"。"""

    def __init__(self, policy_path: str):
        with open(policy_path, "r", encoding="utf-8") as f:
            self._policy = json.load(f)
        self._by_action = {a["action"]: a for a in self._policy.get("actions", [])}

    def get(self, action: str) -> Optional[Dict[str, Any]]:
        return self._by_action.get(action)

    @property
    def raw(self) -> Dict[str, Any]:
        return self._policy

    def allowed(
        self, action: str, user: Optional[Dict[str, Any]], verified_identity: bool
    ) -> Tuple[bool, str]:
        """返回 (是否允许, 说明/策略条款)。

        user 为 None 表示尚未查询员工目录 —— 需要用户上下文的动作一律不允许。
        """
        rule = self._by_action.get(action)
        if rule is None:
            return False, f"unknown_action:{action}（不在策略白名单中，默认禁止）"
        if not rule.get("agent_allowed", False):
            return False, f"{action}:{rule.get('requires', 'human_approval')}"
        # 逐条解释 conditions；遇到不认识的条件按 fail-closed 处理
        user = user or {}
        for cond in rule.get("conditions", []):
            ok, why = _eval_condition(cond, user, verified_identity)
            if not ok:
                return False, f"{action}:condition_not_met:{why}"
        return True, f"{action}:allowed"

    def summary(self) -> Dict[str, Any]:
        """给 LLM 看的策略总纲。"""
        allowed = [
            {"action": a["action"], "conditions": a.get("conditions", [])}
            for a in self._policy.get("actions", [])
            if a.get("agent_allowed")
        ]
        forbidden = [
            {"action": a["action"], "requires": a.get("requires", "human_approval")}
            for a in self._policy.get("actions", [])
            if not a.get("agent_allowed")
        ]
        return {
            "agent_can": allowed,
            "agent_cannot": forbidden,
            "verification": self._policy.get("verification", {}),
            "note": "拿不准的动作先查本策略；execute_action 会做二次强制校验",
        }


def _eval_condition(
    cond: str, user: Dict[str, Any], verified_identity: bool
) -> Tuple[bool, str]:
    """解释策略条件字符串。只认已知的几种，其余 fail-closed。"""
    if cond == "identity_verified":
        return verified_identity, "identity_verified=False（身份未核验）"
    if cond == "role != executive":
        role = user.get("role", "")
        return role != "executive", f"role={role or 'unknown'} 为 executive"
    if cond == "role == employee":
        role = user.get("role", "")
        return role == "employee", f"role={role or 'unknown'} != employee"
    return False, f"unrecognized_condition:{cond}（未知条件按禁止处理）"


class CheckPolicyTool(Tool):
    name = "check_policy"
    description = (
        "查询策略：agent 能直接做什么、什么必须人工审批。"
        "执行敏感动作前先查这里确认边界。topic 留空返回策略总纲。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "动作关键词，如 mfa_reset / password / access。留空返回总纲",
            }
        },
        "required": [],
    }

    def __init__(self, engine: PolicyEngine):
        self._engine = engine

    def run(self, ctx: ToolContext, topic: str = "") -> Dict[str, Any]:
        result = {"source": "policy"}
        if not topic:
            result.update(self._engine.summary())
            return result
        key = topic.lower()
        hits = [
            a
            for a in self._engine.raw.get("actions", [])
            if key in a["action"].lower() or key in a.get("description", "").lower()
        ]
        # 上下文感知：已核验身份的用户角色会影响适用条款
        # 例：承包商申请任何权限，都额外命中 grant_access_contractor 这条审批条款
        # check_policy 是信息性软引导，已核验取可信主体，未核验但已查过目录时
        # 仍用目录结果的角色，让 LLM 尽早看到 contractor 审批路径
        user_role = (
            getattr(ctx.state, "verified_subject", None)
            or getattr(ctx.state, "user", None)
            or {}
        ).get("role", "")
        if user_role == "contractor":
            extra = [
                a for a in self._engine.raw.get("actions", [])
                if a["action"] == "grant_access_contractor" and a not in hits
            ]
            hits = hits + extra
        result["topic"] = topic
        result["actions"] = hits
        result["match_count"] = len(hits)
        return result
