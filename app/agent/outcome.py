"""OutcomeEngine：最终结论审查器（升级判断规则引擎）。

核心原则：LLM 的结论不直接采信，必须过确定性规则审查 ——
- 权限边界是代码说了算，即使 LLM 自报 confidence=0.99 也拦得住
- LLM 自报 confidence 只用于"知识性不确定"的软判断
- 摘要完整性由模板兜底，不依赖 LLM 自觉

五条规则（顺序执行）：
1. 硬边界：决议中的动作被 policy 禁止 → 强制升级（policy_forbidden）
2. 身份门槛：账号类动作但身份未核验 → 改写为追问；追问 2 次仍未核验 → 升级
3. 置信度地板：低置信 → 先追问；追问过仍无新证据 → 升级
4. 防追问循环：连续 2 次 need_info 且无新工具证据 → 升级（clarification_loop）
5. 摘要补全：escalated 但摘要缺要素 → 从状态卡模板化补全
"""
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.state import SessionState
from app.tools.policy import PolicyEngine

VALID_STATES = {"resolved", "pending_user", "need_info", "escalated"}
VALID_REASONS = {
    "policy_forbidden", "identity_unverified", "low_confidence_no_progress",
    "clarification_loop", "max_steps_exceeded", "out_of_scope", "user_request",
    "tool_failure",
}

# 需要身份核验的动作（与 policy.json conditions 对应）
IDENTITY_ACTIONS = {"reset_password", "unlock_account", "grant_standard_software"}

# 只读诊断工具：调查手段而非策略治理的"动作"。LLM 偶尔会把它们误声明进
# actions_taken，不应触发策略硬边界（真正有副作用的动作由 execute_action 硬拦截兜底）
READONLY_TOOLS = {"search_kb", "get_system_status", "lookup_user", "search_tickets", "check_policy"}


@dataclass
class Verdict:
    """审查结论：pass 原样通过 / rewrite 改写后通过 / escalate 强制升级。"""

    action: str  # pass | rewrite | escalate
    final: Dict[str, Any]
    reason: str = ""
    notes: List[str] = field(default_factory=list)


class OutcomeEngine:
    def __init__(self, policy: PolicyEngine, confidence_floor: float = 0.7):
        self.policy = policy
        self.confidence_floor = confidence_floor

    # ---- 主入口 ----

    def review(self, final: Dict[str, Any], state: SessionState) -> Verdict:
        state_name = final.get("state", "")
        if state_name not in VALID_STATES:
            return Verdict("rewrite", self.fallback_final(state, "invalid_state"),
                           reason="invalid_state", notes=[f"非法 state={state_name}"])
        conf = _as_float(final.get("confidence"), 0.5)

        # 规则 1：硬边界 —— 决议中的动作被 policy 禁止 → 无视 confidence 强制升级
        for act in final.get("actions_taken", []) or []:
            if not isinstance(act, dict):
                continue
            action_name = act.get("action", "")
            # 只读诊断工具不是"动作"，跳过策略判定（避免把调查行为误判为越权）
            if not action_name or action_name in READONLY_TOOLS:
                continue
            # 动作还没真正执行成功过（execute_action 返回 ok）的才拦截已执行声明：
            # 这里只拦"声明已做但策略不允许"的情况，实际执行拦截在 execute_action 里
            user = state.user or {}
            allowed, clause = self.policy.allowed(
                action_name, user, state.issue.verified_identity
            )
            if not allowed and action_name:
                return self._escalate(
                    final, state, reason="policy_forbidden",
                    note=f"动作 {action_name} 被策略禁止（{clause}），已强制升级",
                    forbidden_action=action_name,
                )

        # 规则 2：身份门槛 —— resolved/pending_user 携带账号类动作但身份未核验
        if state_name in ("resolved", "pending_user") and self._needs_identity(final):
            if not state.issue.verified_identity:
                if state.identity_ask_count >= 2:
                    return self._escalate(
                        final, state, reason="identity_unverified",
                        note="两次索要身份信息仍未核验，强制升级",
                    )
                state.identity_ask_count += 1
                gap = "employee_id（工号）"
                if gap not in state.clarified_gaps:
                    state.clarified_gaps.append(gap)
                rewritten = dict(final)
                rewritten["state"] = "need_info"
                rewritten["confidence"] = min(conf, 0.6)
                rewritten["user_message"] = (
                    "进行账号操作前，我需要先核验你的身份。请提供你的工号（employee_id）。"
                )
                return Verdict("rewrite", rewritten, reason="identity_gate",
                               notes=["身份未核验，改写为追问"])

        # 规则 3：置信度地板 —— 低置信先追问，追问过无进展则升级
        if state_name == "resolved" and conf < self.confidence_floor:
            if state.clarified_gaps and not state.new_tool_evidence_since(state.last_clarify_turn):
                return self._escalate(
                    final, state, reason="low_confidence_no_progress",
                    note=f"confidence={conf:.2f} 低于地板且追问后无新证据",
                )
            rewritten = dict(final)
            rewritten["state"] = "need_info"
            return Verdict("rewrite", rewritten, reason="confidence_floor",
                           notes=[f"confidence={conf:.2f} < {self.confidence_floor}，改写为追问"])

        # 规则 4：防追问循环 —— 本次已是第 consecutive+1 次连续追问，
        # 即本次至少是第 2 次；且期间无新工具证据 → 升级（clarification_loop）
        if state_name == "need_info":
            if state.consecutive_need_info >= 1 and not state.new_tool_evidence_since(
                state.last_clarify_turn
            ):
                return self._escalate(
                    final, state, reason="clarification_loop",
                    note="连续追问仍无法定位问题，强制升级",
                )

        # 规则 5：升级摘要补全
        if state_name == "escalated":
            final = self.complete_summary(final, state)

        return Verdict("pass", final)

    # ---- 升级构造与摘要补全 ----

    def _escalate(
        self, final: Dict[str, Any], state: SessionState, reason: str, note: str,
        forbidden_action: str = "",
    ) -> Verdict:
        escalated = dict(final)
        escalated["state"] = "escalated"
        esc = dict(escalated.get("escalation") or {})
        esc["reason"] = reason
        if forbidden_action:
            esc["forbidden_action"] = forbidden_action
        escalated["escalation"] = esc
        escalated = self.complete_summary(escalated, state, force_reason=reason)
        escalated["user_message"] = escalated.get("user_message") or (
            "这个问题超出了我能自动处理的范围，我已经把完整的调查记录转给人工支持。"
        )
        return Verdict("escalate", escalated, reason=reason, notes=[note])

    def complete_summary(
        self,
        final: Dict[str, Any],
        state: SessionState,
        force_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        """把升级摘要模板化补全 —— 摘要从状态卡合成，不依赖 LLM 自觉。

        保证"带完整上下文交接"这一硬性要求 100% 达标。
        """
        issue = state.issue
        u = state.user or {}
        esc = dict(final.get("escalation") or {})
        reason = force_reason or esc.get("reason") or "unspecified"
        if reason not in VALID_REASONS:
            reason = "unspecified"
        urgency = esc.get("urgency") if esc.get("urgency") in ("low", "medium", "high") else "medium"

        confirmed = [h.text for h in state.hypotheses if h.status == "confirmed"]
        rejected = [h.text for h in state.hypotheses if h.status == "rejected"]
        evidence = list(dict.fromkeys(final.get("evidence", []) or []))
        attempted = issue.attempted or []
        suggestion = _extract_suggestion(final.get("user_message", ""))

        # 确定性收集"被策略拒绝/禁止的动作名"，保证升级工单点名具体动作：
        # 1) 规则引擎/对账路径写入的 forbidden_action
        # 2) execute_action 被拒的调用
        # 3) check_policy 返回结果里被标"禁止"的动作（LLM 查了边界但未尝试动作的场景）
        names: List[str] = []
        if esc.get("forbidden_action"):
            names.append(esc["forbidden_action"])
        for r in state.tool_records:
            if r.name == "execute_action" and r.status == "error":
                a = (r.args or {}).get("action")
                if a:
                    names.append(a)
            elif r.name == "check_policy" and r.status == "ok" and r.summary:
                names.extend(re.findall(r"([a-z_]+)=禁止", r.summary))
        # 兜底：已核验用户为 contractor 时，权限类申请必然命中审批条款
        # （LLM 可能先查策略后核验身份，导致 check_policy 时角色感知未生效；
        #   问题卡 category 由 apply_final 在 review 之后合并，此处读 update 里的 category）
        issue_category = (
            (final.get("issue_update") or {}).get("category") or issue.category
        )
        if (state.user or {}).get("role") == "contractor" and issue_category == "access":
            names.append("grant_access_contractor")
        names = list(dict.fromkeys(n for n in names if n))
        trigger_line = f"\n- 触发动作: {', '.join(names)}" if names else ""

        summary = (
            f"【升级工单】\n"
            f"- 员工: {u.get('name', issue.user_id or '未知')}"
            f"（{issue.user_id or '未提供工号'}, {u.get('department', '?')}/{u.get('role', '?')}）, "
            f"身份核验: {'已通过' if issue.verified_identity else '未完成'}\n"
            f"- 问题描述: {issue.description or '（用户未详述）'}\n"
            f"- 症状: {', '.join(issue.symptoms) or '无'}\n"
            f"- 已确认假设: {'; '.join(confirmed) or '无'}\n"
            f"- 已排除假设: {'; '.join(rejected) or '无'}\n"
            f"- 已尝试方案: {'; '.join(attempted) or '无'}\n"
            f"- 关键证据: {'; '.join(evidence) or '无'}\n"
            f"- 升级原因: {reason}{trigger_line}\n"
            f"- 未解决疑点: {'; '.join(issue.unresolved_questions) or '无'}\n"
            f"- 建议处理: {suggestion or '见对话记录'}"
        )
        esc["summary"] = summary
        esc["reason"] = reason
        esc["urgency"] = urgency
        out = dict(final)
        out["escalation"] = esc
        # 证据合并：LLM 给的 + 状态卡里工具记录的来源
        return out

    # ---- 辅助 ----

    def _needs_identity(self, final: Dict[str, Any]) -> bool:
        for act in final.get("actions_taken", []) or []:
            if isinstance(act, dict) and act.get("action") in IDENTITY_ACTIONS:
                return True
        return False

    def fallback_final(self, state: SessionState, why: str) -> Dict[str, Any]:
        """格式错误且重试用尽时的确定性兜底：按状态卡生成保守结论。"""
        return {
            "state": "need_info",
            "confidence": 0.3,
            "user_message": (
                "抱歉，我刚才处理你的请求时遇到了一点问题。"
                "能麻烦你换个方式再描述一下问题吗？"
            ),
            "issue_update": {"unresolved_questions": [f"output_parse_failed:{why}"]},
            "actions_taken": [],
            "evidence": [],
        }


def force_escalation_final(state: SessionState, reason: str) -> Dict[str, Any]:
    """步数耗尽等场景的强制升级结论（不经 LLM）。"""
    return {
        "state": "escalated",
        "confidence": 0.5,
        "user_message": (
            "这个问题我调查了几轮还没能定位，为了不耽误你，"
            "我已经把目前的调查记录整理成工单转给人工支持了。"
        ),
        "issue_update": {},
        "actions_taken": [],
        "evidence": [f"tool: {r.name}({r.status})" for r in state.tool_records[-5:]],
        "escalation": {"summary": "", "reason": reason, "urgency": "medium"},
    }


def _as_float(v: Any, default: float) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _extract_suggestion(user_message: str) -> str:
    """从给用户的回复里提取一句话建议（截断），供升级摘要引用。"""
    if not user_message:
        return ""
    line = user_message.strip().split("\n")[0]
    return line[:80]
