"""OutcomeEngine：最终结论审查器（升级判断规则引擎）。

核心原则：LLM 的结论不直接采信，必须过确定性规则审查 ——
- 权限边界是代码说了算，即使 LLM 自报 confidence=0.99 也拦得住
- LLM 自报 confidence 只用于"知识性不确定"的软判断
- 摘要完整性由模板兜底，不依赖 LLM 自觉

六条规则（顺序执行）：
0. 证据冲突护栏：resolved/pending_user 带未解决冲突 → 改写为 need_info
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

        # 规则 0：证据冲突护栏 —— resolved/pending_user 不允许带未解决的证据冲突，
        # 否则强制改为 need_info 让用户澄清冲突，避免把矛盾包装成确定答案
        active_conflicts = state.issue.active_conflicts
        if state_name in ("resolved", "pending_user") and active_conflicts:
            rewritten = dict(final)
            rewritten["state"] = "need_info"
            rewritten["confidence"] = min(conf, 0.5)
            rewritten["user_message"] = (
                "我查到的信息之间有矛盾，需要再确认一下："
                + "；".join(active_conflicts[:2])
                + "。请告诉我哪个更符合你实际看到的情况？"
            )
            return Verdict("rewrite", rewritten, reason="conflict_guard",
                           notes=[f"未解决冲突: {active_conflicts}"])

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
            # Step 4：使用有效的核验记录（而非仅布尔值），与 actions.py 闸门保持一致
            user = state.verified_subject or {}
            verified = state.has_valid_identity_verification()
            # 身份类动作在核验未通过时不可能被 execute_action 执行成功（闸门已拒），
            # 此时 actions_taken 中的声明是 LLM 误报。不按"策略禁止"升级，
            # 交由规则 2 追问缺失的身份信息（设置 pending_identity_question）。
            if action_name in IDENTITY_ACTIONS and not verified:
                continue
            allowed, clause = self.policy.allowed(
                action_name, user, verified
            )
            if not allowed and action_name:
                return self._escalate(
                    final, state, reason="policy_forbidden",
                    note=f"动作 {action_name} 被策略禁止（{clause}），已强制升级",
                    forbidden_action=action_name,
                )

        # 规则 1.5：查无此人的身份核验失败处理 —— 用户明确提供了工号但 lookup_user
        # 返回 0 命中，说明工号不存在。第一次发生时让用户重新确认工号（need_info），
        # 若用户仍无法提供有效工号（identity_ask_count >= 1）则强制升级。
        # 此规则不依赖 LLM 输出的 state，避免 LLM 倾向追问时绕过身份核验失败的升级路径。
        if state.identity_lookup_no_match and not state.issue.verified_identity:
            if state.identity_ask_count >= 1:
                return self._escalate(
                    final, state, reason="identity_unverified",
                    note="员工目录查无此人且用户无法提供有效工号，身份核验失败，转人工处理",
                )
            # 第一次查无此人：让用户重新确认工号，不立即升级
            state.identity_ask_count += 1
            rewritten = dict(final)
            rewritten["state"] = "need_info"
            rewritten["confidence"] = min(conf, 0.5)
            rewritten["user_message"] = (
                "员工目录里没有找到你提供的工号，请确认工号是否输入正确（格式如 E1001），"
                "或提供你的注册邮箱以便核实。"
            )
            return Verdict("rewrite", rewritten, reason="identity_lookup_failed",
                           notes=["工号查无此人，要求用户重新确认"])

        # 规则 2：身份门槛 —— resolved/pending_user 携带账号类动作但身份未核验
        if state_name in ("resolved", "pending_user") and self._needs_identity(final):
            # Step 4：核验必须"仍有效"才算已核验（部门声明被撤回则视为未核验）
            if not state.has_valid_identity_verification():
                if state.identity_ask_count >= 2:
                    return self._escalate(
                        final, state, reason="identity_unverified",
                        note="两次索要身份信息仍未核验，强制升级",
                    )
                state.identity_ask_count += 1
                # Step 3：根据缺失字段决定追问内容，并设置 pending_identity_question，
                # 使用户下一轮的裸回答能被 _extract_department 接受为本人声明。
                has_eid = bool(state.last_claimed_eid)
                has_dept = state.department_claim is not None
                if not has_eid:
                    gap = "employee_id（工号）"
                    ask_msg = (
                        "进行账号操作前，我需要先核验你的身份。"
                        "请提供你的工号（employee_id）和所在部门（如 engineering / finance）。"
                    )
                    # 工号优先：先问工号；用户回答工号后 _save_identity_claims 会自动
                    # 设置 department 的 pending 问题。
                    state.pending_identity_question = {
                        "field": "employee_id", "subject": "self",
                        "asked_turn": state.turn_count,
                        "identity_epoch": state.identity_epoch,
                    }
                elif not has_dept:
                    gap = "department（部门）"
                    ask_msg = (
                        "进行账号操作前，我还需要确认你的部门。"
                        "请告诉我你所在的部门（如 engineering / finance）。"
                    )
                    state.pending_identity_question = {
                        "field": "department", "subject": "self",
                        "asked_turn": state.turn_count,
                        "identity_epoch": state.identity_epoch,
                    }
                else:
                    # 两者都有但与目录不匹配 → 身份核验失败，升级人工处理（G03：
                    # 工号与部门不匹配时不得声称成功，必须转人工核实）。
                    return self._escalate(
                        final, state, reason="identity_unverified",
                        note="身份信息与目录记录不匹配，转人工核实",
                    )
                if gap not in state.clarified_gaps:
                    state.clarified_gaps.append(gap)
                rewritten = dict(final)
                rewritten["state"] = "need_info"
                rewritten["confidence"] = min(conf, 0.6)
                rewritten["user_message"] = ask_msg
                return Verdict("rewrite", rewritten, reason="identity_gate",
                               notes=[f"身份未核验，追问{gap}"])

        # 规则 3：置信度地板 —— 低置信先追问，追问过无进展则升级
        if state_name == "resolved" and conf < self.confidence_floor:
            if state.clarified_gaps and not state.has_new_progress_since(state.last_clarify_turn):
                return self._escalate(
                    final, state, reason="low_confidence_no_progress",
                    note=f"confidence={conf:.2f} 低于地板且追问后无新进展",
                )
            rewritten = dict(final)
            rewritten["state"] = "need_info"
            return Verdict("rewrite", rewritten, reason="confidence_floor",
                           notes=[f"confidence={conf:.2f} < {self.confidence_floor}，改写为追问"])

        # 规则 4：防追问循环 —— 连续追问且期间无新进展（工具结果或用户新事实）→ 升级
        # C01：用户补充地点、版本、错误码等新事实也算进展，不能只看工具调用。
        if state_name == "need_info":
            if state.consecutive_need_info >= 1 and not state.has_new_progress_since(
                state.last_clarify_turn
            ):
                return self._escalate(
                    final, state, reason="clarification_loop",
                    note="连续追问仍无新进展，强制升级",
                )

        # 规则 5：升级摘要补全
        if state_name == "escalated":
            # 查无此人（用户提供的工号在目录中 0 命中）→ 身份核验失败是更准确的升级原因。
            # identity_lookup_no_match 仅在用户明确提供工号且查无此人时置位，
            # 因此可以安全地覆盖任何泛化原因。
            if state.identity_lookup_no_match:
                esc = dict(final.get("escalation") or {})
                esc["reason"] = "identity_unverified"
                final = dict(final)
                final["escalation"] = esc
            final = self.complete_summary(final, state)
            # 同步话术：state=escalated 时回复统一为"已转人工"，不保留 LLM 可能的追问
            final["user_message"] = (
                final.get("user_message", "")
                if "人工" in (final.get("user_message") or "")
                else "这个问题超出了我能自动处理的范围，我已经把完整的调查记录转给人工 IT 支持。"
            )

        # 身份声明真实性护栏：若核验未通过，回复中不得声称"身份已核实/已核对"，
        # 避免 LLM 把未完成的核验说成已完成（与 D05 凭证声明对账同理）。
        # 注意：只匹配"已+动词"或"动词+完成态(完毕/通过/成功)"，
        # 不删裸"身份核验"——否则会误伤"用于身份核验"、"身份核验未通过"等合法表达。
        if not state.has_valid_identity_verification():
            import re as _re
            msg = final.get("user_message", "") or ""
            fake_identity_patterns = [
                r"身份已(?:核实|核对|验证|核验|确认)(?:完毕|通过|成功)?",
                r"身份(?:核实|核对|验证|核验|确认)(?:完毕|通过|成功)",
                r"已核实(?:了)?身份",
                r"已核对(?:了)?(?:你的)?身份",
                r"身份信息已(?:核实|核对|验证)(?:完毕|通过)?",
                r"身份信息(?:核实|核对|验证)(?:完毕|通过)",
            ]
            for pat in fake_identity_patterns:
                msg = _re.sub(pat, "", msg)
            msg = _re.sub(r"[，；;]\s*[，；;]", "，", msg)
            msg = _re.sub(r"\s{2,}", " ", msg).strip(" ，；;。")
            final = dict(final)
            final["user_message"] = msg

        return Verdict("pass", final)

    # ---- 升级构造与摘要补全 ----

    def _escalate(
        self, final: Dict[str, Any], state: SessionState, reason: str, note: str,
        forbidden_action: str = "",
    ) -> Verdict:
        # 查无此人（用户提供的工号 0 命中）→ 身份核验失败是更准确的升级原因
        if state.identity_lookup_no_match:
            reason = "identity_unverified"
            note = "员工目录查无此人，身份核验失败，转人工处理"
        escalated = dict(final)
        escalated["state"] = "escalated"
        esc = dict(escalated.get("escalation") or {})
        esc["reason"] = reason
        if forbidden_action:
            esc["forbidden_action"] = forbidden_action
        escalated["escalation"] = esc
        escalated = self.complete_summary(escalated, state, force_reason=reason)
        # 升级话术必须与状态一致：不保留 LLM 原回复中可能含有的追问/指令，
        # 统一用"已转人工"口径，避免 state=escalated 但回复仍要求用户回答（C04）
        escalated["user_message"] = (
            "这个问题超出了我能自动处理的范围，我已经把完整的调查记录转给人工 IT 支持，"
            "他们会尽快联系你。"
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
        u = state.verified_subject or {}
        esc = dict(final.get("escalation") or {})
        reason = force_reason or esc.get("reason") or "unspecified"
        if reason not in VALID_REASONS:
            reason = "unspecified"
        urgency = esc.get("urgency") if esc.get("urgency") in ("low", "medium", "high") else "medium"

        confirmed = [h.text for h in state.hypotheses if h.status == "confirmed"]
        rejected = [h.text for h in state.hypotheses if h.status == "rejected"]
        # 防御：evidence 可能是非可迭代类型（如 int/str），需先归一化为列表
        ev_raw = final.get("evidence", [])
        if isinstance(ev_raw, list):
            evidence = list(dict.fromkeys(e for e in ev_raw if isinstance(e, str)))
        elif isinstance(ev_raw, str):
            evidence = [ev_raw] if ev_raw else []
        else:
            evidence = []
        attempted = issue.attempted or []
        suggestion = _extract_suggestion(final.get("user_message", "") or "")

        # 确定性收集"被策略拒绝/禁止的动作名"，保证升级工单点名具体动作：
        # 1) 规则引擎/对账路径写入的 forbidden_action
        # 2) execute_action 被拒的调用（仅限本轮，避免把上一问题的失败动作
        #    混入当前问题的升级工单，防止"此前成功的操作被误报为失败"）
        # 3) check_policy 返回结果里被标"禁止"的动作（LLM 查了边界但未尝试动作的场景）
        names: List[str] = []
        if esc.get("forbidden_action"):
            names.append(esc["forbidden_action"])
        for r in state.tool_records:
            # 只统计本轮（r.turn == state.turn_count）的被拒动作，跨问题不串扰
            if r.turn != state.turn_count:
                continue
            if r.name == "execute_action" and r.status == "error":
                a = (r.args or {}).get("action")
                if a:
                    names.append(a)
            elif r.name == "check_policy" and r.status == "ok" and r.summary:
                names.extend(re.findall(r"([a-z_]+)=禁止", r.summary))
        # 兜底：目录显示用户为 contractor 时，权限类申请必然命中审批条款。
        # 这里用 verified_subject or state.user：已核验取可信主体，未核验但已查过目录时
        # 仍能从目录结果识别 contractor 角色，确保升级工单点名 contractor 审批条款。
        # （LLM 可能先查策略后核验身份，导致 check_policy 时角色感知未生效；
        #   问题卡 category 由 apply_final 在 review 之后合并，此处读 update 里的 category）
        issue_category = (
            (final.get("issue_update") or {}).get("category") or issue.category
        )
        contractor_role = (state.verified_subject or state.user or {}).get("role", "")
        if contractor_role == "contractor" and issue_category == "access":
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
            f"- 证据冲突: {'; '.join(issue.active_conflicts) or '无'}\n"
            f"- 已尝试方案: {'; '.join(attempted) or '无'}\n"
            f"- 关键证据: {'; '.join(evidence) or '无'}\n"
            f"- 升级原因: {reason}{trigger_line}\n"
            f"- 未解决疑点: {'; '.join(q.text for q in issue.unresolved_questions) or '无'}\n"
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
    if not isinstance(user_message, str):
        user_message = str(user_message)
    line = user_message.strip().split("\n")[0]
    return line[:80]


def normalize_final(final: Any) -> Optional[Dict[str, Any]]:
    """对 LLM 最终输出做契约级类型校验与归一化。

    F05.2：合法 JSON 但字段类型错误（confidence='abc'、evidence=42、
    actions_taken='x'、issue_update=None）必须安全降级而非崩溃。

    归一化策略（state 合法时静默修复类型，不触发重试）：
    - confidence 非数值 → 兜底 0.5；超出 [0,1] 范围 → 钳制
    - user_message 非字符串/空 → 兜底为默认追问话术
    - evidence 非字符串列表 → 过滤为字符串列表（非列表置空）
    - actions_taken 非 dict 列表 → 过滤为 dict 列表；每项 params 非 dict 且非 None → 丢弃该动作
    - issue_update 非 dict → 置空 dict；内部列表字段非列表 → 置空
    - issue_update.hypotheses 每项 status 非法 → 修正为 testing
    - escalation 非 dict（且存在）→ 置空 dict

    返回归一化后的 dict；若 state 字段非法（非字符串或不在 VALID_STATES）
    则返回 None，交由调用方走"有界重试 → fallback_final"路径。
    """
    if not isinstance(final, dict):
        return None
    state_name = final.get("state")
    if not isinstance(state_name, str) or state_name not in VALID_STATES:
        return None

    out = dict(final)
    # confidence：数值兜底 + 范围钳制 [0, 1]
    conf = final.get("confidence", 0.5)
    try:
        conf = float(conf) if conf is not None else 0.5
    except (TypeError, ValueError):
        conf = 0.5
    out["confidence"] = max(0.0, min(1.0, conf))
    # user_message：字符串兜底；空串给默认追问话术，保证"可继续求助"
    um = final.get("user_message", "")
    if not isinstance(um, str):
        um = str(um) if um else ""
    out["user_message"] = um or "我需要再确认一下你的情况，能详细描述一下吗？"
    # evidence：必须是字符串列表
    ev = final.get("evidence", [])
    out["evidence"] = (
        [e for e in ev if isinstance(e, str)] if isinstance(ev, list) else []
    )
    # validation_notes：贯穿 actions_taken / issue_update 等多段校验的字段路径记录。
    # 必须在所有引用之前初始化，否则 actions_taken 校验时会 NameError。
    validation_notes: List[str] = []
    # actions_taken：契约级校验，非法动作直接拒绝（不通过 str()/空字典兜底
    # 把坏输入变成可执行请求）。
    # W04：action=["reset_password"]（list）或 action=null/42/{} 会在后续
    # 集合成员判断或动作执行时崩溃；必须在此处拦截。
    # 校验维度：
    #   - action 必须是非空字符串，且属于支持的动作（IDENTITY_ACTIONS 或 READONLY_TOOLS）
    #   - params 必须是 dict（None 视为空）
    #   - 可执行动作的关键字段类型校验：employee_id 须为 str|None，
    #     grant_standard_software 的 software 须为 str|None
    # 校验失败的动作丢弃，不触发执行；validation_notes 记录字段路径（不含凭据值）。
    SUPPORTED_ACTIONS = IDENTITY_ACTIONS | READONLY_TOOLS
    at = final.get("actions_taken", [])
    if isinstance(at, list):
        cleaned_actions = []
        for idx, a in enumerate(at):
            path = f"actions_taken[{idx}]"
            if not isinstance(a, dict):
                validation_notes.append(f"{path}: expected dict, got {type(a).__name__}")
                continue
            action = a.get("action")
            # action 必须是非空字符串
            if not isinstance(action, str) or not action.strip():
                validation_notes.append(
                    f"{path}.action: expected non-empty str, got {type(action).__name__}"
                )
                continue
            action = action.strip()
            # action 必须是支持的动作名
            if action not in SUPPORTED_ACTIONS:
                validation_notes.append(
                    f"{path}.action: unsupported action {action!r}"
                )
                continue
            params = a.get("params")
            # params 必须是 dict 或 None
            if params is not None and not isinstance(params, dict):
                validation_notes.append(
                    f"{path}.params: expected dict|None, got {type(params).__name__}"
                )
                continue
            params = params or {}
            # 可执行动作的关键字段类型校验（仅校验类型，不做策略判断）
            if action in IDENTITY_ACTIONS:
                eid = params.get("employee_id")
                if eid is not None and not isinstance(eid, str):
                    validation_notes.append(
                        f"{path}.params.employee_id: expected str|None, got {type(eid).__name__}"
                    )
                    continue
                if action == "grant_standard_software":
                    sw = params.get("software")
                    if sw is not None and not isinstance(sw, str):
                        validation_notes.append(
                            f"{path}.params.software: expected str|None, got {type(sw).__name__}"
                        )
                        continue
            # 校验通过：保留原 dict（action 已 strip）
            cleaned = dict(a)
            cleaned["action"] = action
            cleaned["params"] = params
            cleaned_actions.append(cleaned)
        out["actions_taken"] = cleaned_actions
    else:
        validation_notes.append(f"actions_taken: expected list, got {type(at).__name__}")
        out["actions_taken"] = []
    # issue_update：必须是 dict，内部列表字段必须是列表，且逐项校验嵌套结构。
    # X05：conflicts 项的 evidence 若为 int/str 等非列表，merge 时遍历会 TypeError 崩溃。
    # 统一校验：嵌套列表、对象、枚举与数值，校验失败时归一化（丢弃坏项/坏字段），
    # 并在 _validation_notes 中记录具体字段路径，便于追溯（不影响原状态合并）。
    iu = final.get("issue_update", {})
    if isinstance(iu, dict):
        iu = dict(iu)
        # 先把所有应是列表的字段强制成列表（非列表置空）
        for list_key in ("symptoms", "attempted", "unresolved_questions",
                         "conflicts", "resolved_conflicts", "answered_questions"):
            v = iu.get(list_key)
            if v is not None and not isinstance(v, list):
                validation_notes.append(f"issue_update.{list_key}: expected list, got {type(v).__name__}")
                iu[list_key] = []
        # symptoms / attempted：只保留字符串项
        for str_list_key in ("symptoms", "attempted"):
            v = iu.get(str_list_key)
            if isinstance(v, list):
                cleaned = [item for item in v if isinstance(item, str)]
                if len(cleaned) != len(v):
                    validation_notes.append(f"issue_update.{str_list_key}: filtered non-string items")
                iu[str_list_key] = cleaned
        # conflicts：每项是 str 或 dict；dict 的 evidence 必须是字符串列表，
        # status 必须是合法枚举（active / resolved）。
        valid_conflict_status = {"active", "resolved"}
        conflicts = iu.get("conflicts")
        if isinstance(conflicts, list):
            cleaned_conflicts = []
            for idx, item in enumerate(conflicts):
                path = f"issue_update.conflicts[{idx}]"
                if isinstance(item, str):
                    cleaned_conflicts.append(item)
                elif isinstance(item, dict):
                    item = dict(item)
                    ev = item.get("evidence", [])
                    if isinstance(ev, list):
                        item["evidence"] = [e for e in ev if isinstance(e, str)]
                    else:
                        validation_notes.append(f"{path}.evidence: expected list, got {type(ev).__name__}")
                        item["evidence"] = []
                    status = item.get("status")
                    if not isinstance(status, str) or status not in valid_conflict_status:
                        validation_notes.append(f"{path}.status: invalid enum {status!r}, defaulting to 'active'")
                        item["status"] = "active"
                    cleaned_conflicts.append(item)
                else:
                    validation_notes.append(f"{path}: expected str|dict, got {type(item).__name__}")
            iu["conflicts"] = cleaned_conflicts
        # resolved_conflicts / answered_questions：每项是 str 或带 id/text 的 dict
        for resolve_key in ("resolved_conflicts", "answered_questions"):
            v = iu.get(resolve_key)
            if isinstance(v, list):
                cleaned = []
                for idx, item in enumerate(v):
                    path = f"issue_update.{resolve_key}[{idx}]"
                    if isinstance(item, str):
                        cleaned.append(item)
                    elif isinstance(item, dict):
                        # 只保留 id / text 两个字段，丢弃其他意外结构
                        cleaned.append({
                            "id": item.get("id") if isinstance(item.get("id"), str) else "",
                            "text": item.get("text") if isinstance(item.get("text"), str) else "",
                        })
                    else:
                        validation_notes.append(f"{path}: expected str|dict, got {type(item).__name__}")
                iu[resolve_key] = cleaned
        # unresolved_questions：每项是 str 或 dict；dict 的 status 必须是合法枚举
        valid_question_status = {"open", "answered"}
        questions = iu.get("unresolved_questions")
        if isinstance(questions, list):
            cleaned_questions = []
            for idx, item in enumerate(questions):
                path = f"issue_update.unresolved_questions[{idx}]"
                if isinstance(item, str):
                    cleaned_questions.append(item)
                elif isinstance(item, dict):
                    item = dict(item)
                    # Z02：text 可能是非字符串（如 int 42），必须归一化为字符串，
                    # 否则后续 "；".join 时 TypeError: expected str instance, int found
                    text = item.get("text")
                    if not isinstance(text, str):
                        validation_notes.append(f"{path}.text: expected str, got {type(text).__name__}, coercing")
                        item["text"] = str(text) if text is not None else ""
                    status = item.get("status")
                    if not isinstance(status, str) or status not in valid_question_status:
                        validation_notes.append(f"{path}.status: invalid enum {status!r}, defaulting to 'open'")
                        item["status"] = "open"
                    cleaned_questions.append(item)
                else:
                    validation_notes.append(f"{path}: expected str|dict, got {type(item).__name__}")
            iu["unresolved_questions"] = cleaned_questions
        # hypotheses：每项必须是 dict，status 必须合法，evidence 必须是字符串列表
        hyps = iu.get("hypotheses")
        if isinstance(hyps, list):
            valid_status = {"testing", "confirmed", "rejected", "conflicted"}
            cleaned_hyps = []
            for idx, h in enumerate(hyps):
                path = f"issue_update.hypotheses[{idx}]"
                if not isinstance(h, dict):
                    validation_notes.append(f"{path}: expected dict, got {type(h).__name__}")
                    continue
                h = dict(h)
                hstatus = h.get("status")
                if not isinstance(hstatus, str) or hstatus not in valid_status:
                    validation_notes.append(f"{path}.status: invalid enum {hstatus!r}, defaulting to 'testing'")
                    h["status"] = "testing"
                ev = h.get("evidence", [])
                if isinstance(ev, list):
                    h["evidence"] = [e for e in ev if isinstance(e, str)]
                else:
                    validation_notes.append(f"{path}.evidence: expected list, got {type(ev).__name__}")
                    h["evidence"] = []
                cleaned_hyps.append(h)
            iu["hypotheses"] = cleaned_hyps
        elif hyps is not None:
            validation_notes.append(f"issue_update.hypotheses: expected list, got {type(hyps).__name__}")
            iu["hypotheses"] = []
        out["issue_update"] = iu
    else:
        validation_notes.append(f"issue_update: expected dict, got {type(iu).__name__}")
        out["issue_update"] = {}
    # 记录校验发现的字段路径（供调试/审计，不参与业务逻辑）
    if validation_notes:
        out["_validation_notes"] = validation_notes
    # escalation：若存在必须是 dict
    esc = final.get("escalation")
    if esc is not None and not isinstance(esc, dict):
        out["escalation"] = {}
    return out
