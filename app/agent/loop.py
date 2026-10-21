"""AgentLoop：手写 ReAct 主循环 —— 全项目的核心中枢。

一次 run_turn 的生命周期：
  用户消息 → [LLM 调用 ↔ 工具执行] 循环（有步数上限）
           → 最终 JSON 契约 → 动作对账（声明了就真执行）
           → OutcomeEngine 规则审查（LLM 结论不直接采信）
           → 状态落卡 + trace 记录 → TurnResult

可靠性护栏：
- MAX_STEPS_PER_TURN：防死循环/控成本，耗尽强制升级
- 工具失败回填结构化错误，LLM 可换路径；连续失败注入换路径提示
- JSON 解析失败：宽松解析 → 一次重试 → 状态卡兜底
- LLM 网络故障：优雅降级提示，不让会话崩掉
"""
import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.agent.outcome import READONLY_TOOLS, OutcomeEngine, force_escalation_final
from app.agent.prompts import build_messages, build_system_prompt, summarize_tool_result
from app.agent.state import SessionState, ToolRecord, new_session_id
from app.llm.base import BaseLLM, ToolCall
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolContext, ToolRegistry

MAX_CONSECUTIVE_TOOL_ERRORS = 2


@dataclass
class TurnResult:
    """一次用户轮次的输出（API 与评测共同消费的结构）。"""

    session_id: str
    reply: str
    state: str  # resolved | pending_user | need_info | escalated
    confidence: float
    evidence: List[str] = field(default_factory=list)
    tools_used: List[Dict[str, Any]] = field(default_factory=list)
    escalation: Optional[Dict[str, Any]] = None
    latency_ms: int = 0
    usage: Dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


class AgentLoop:
    def __init__(
        self,
        llm: BaseLLM,
        registry: ToolRegistry,
        store,
        outcome: OutcomeEngine,
        policy: PolicyEngine,
        settings,
        tracer=None,
    ):
        self.llm = llm
        self.registry = registry
        self.store = store
        self.outcome = outcome
        self.policy = policy
        self.settings = settings
        self.tracer = tracer

    # ---- 对外主入口 ----

    def run_turn(self, session_id: str, user_message: str) -> TurnResult:
        state = self.store.get_or_create(session_id)
        t0 = time.monotonic()

        # 会话已终结（升级/解决）→ 短路返回，不再消耗 LLM
        if state.escalated or state.resolved:
            return self._closed_turn(state, t0)

        state.append_message("user", user_message)
        state.turn_count += 1
        state.final_retry_used = False
        state.consecutive_tool_errors = 0

        turn_tools: List[Dict[str, Any]] = []
        final: Optional[Dict[str, Any]] = None

        for _step in range(self.settings.agent_max_steps_per_turn):
            resp = self._call_llm(state)
            if isinstance(resp, str):  # LLM 故障的优雅降级
                return self._graceful_failure(state, resp, t0)
            state.append_usage(resp.usage)

            # 分支 1：工具调用 → 执行并回填结果，继续循环
            if resp.tool_calls:
                state.append_assistant_tool_calls(resp)
                for tc in resp.tool_calls:
                    rec, result = self._execute_tool(tc, state, turn_tools)
                    state.append_tool_result(tc.id, result)
                    state.tool_records.append(rec)
                    state.consecutive_tool_errors = (
                        state.consecutive_tool_errors + 1 if rec.status == "error" else 0
                    )
                if state.consecutive_tool_errors == MAX_CONSECUTIVE_TOOL_ERRORS:
                    state.append_message(
                        "system",
                        "【系统提示】工具已连续失败两次。请换一条调查路径；"
                        "若无路可走，如实告知用户并置 state=escalated。",
                    )
                continue

            # 分支 2：最终答案 → 解析 JSON 契约
            final = parse_final_json(resp.content)
            if final is None:
                if not state.final_retry_used:
                    state.final_retry_used = True
                    state.append_message(
                        "system",
                        "【系统提示】你的上一条输出不是合法的契约 JSON。"
                        "请严格按输出契约重新作答，只输出一个 JSON 对象。",
                    )
                    continue
                final = self.outcome.fallback_final(state, "parse_failed_after_retry")
                state.append_message("assistant", resp.content or "")
            break

        if final is None:  # 步数耗尽 → 强制升级（摘要由状态卡合成，不依赖 LLM）
            final = force_escalation_final(state, "max_steps_exceeded")

        final = self._reconcile_actions(final, state)  # 声明的动作必须真实执行
        verdict = self.outcome.review(final, state)
        final = verdict.final

        # 状态联动：追问计数 / 澄清标记
        if final.get("state") == "need_info":
            state.consecutive_need_info += 1
            state.last_clarify_turn = state.turn_count
        else:
            state.consecutive_need_info = 0

        state.append_message("assistant", final.get("user_message", ""))
        state.apply_final(final)
        if final.get("state") == "escalated":
            state.escalation_ticket = {
                "id": f"ESC-{state.session_id[:4].upper()}-{state.turn_count:03d}",
                "reason": final.get("escalation", {}).get("reason", "unspecified"),
                "urgency": final.get("escalation", {}).get("urgency", "medium"),
                "summary": final.get("escalation", {}).get("summary", ""),
            }

        latency_ms = int((time.monotonic() - t0) * 1000)
        result = self._build_turn_result(state, final, turn_tools, latency_ms)
        if self.tracer:
            self.tracer.log_turn(state, final, verdict, result)
        return result

    def new_session(self) -> str:
        """创建新会话（评测用），返回 session_id。"""
        sid = new_session_id()
        self.store.get_or_create(sid)
        return sid

    # ---- 内部步骤 ----

    def _call_llm(self, state: SessionState):
        """带兜底的 LLM 调用；失败时返回错误字符串而不是抛异常。"""
        system_prompt = build_system_prompt(state, self.policy.summary())
        messages = build_messages(state, system_prompt, self.settings.agent_max_context_messages)
        try:
            return self.llm.complete(
                messages, tools=self.registry.schemas(), temperature=self.settings.llm_temperature
            )
        except Exception as exc:  # noqa: BLE001 —— 网络/限流/配额等一律优雅降级
            return f"llm_unavailable: {type(exc).__name__}: {exc}"

    def _execute_tool(self, tc: ToolCall, state: SessionState, turn_tools: List[Dict[str, Any]]):
        """执行单个工具，返回 (ToolRecord, 回填给 LLM 的结果)。"""
        t0 = time.monotonic()
        result = self.registry.safe_execute(tc, ToolContext(settings=self.settings, state=state))
        ms = int((time.monotonic() - t0) * 1000)
        status = result.get("status", "error")
        data = result.get("data", result)
        summary = (
            summarize_tool_result(tc.name, data)
            if status == "ok" and isinstance(data, dict)
            else str(result.get("error", ""))[:120]
        )
        rec = ToolRecord(turn=state.turn_count, name=tc.name, args=tc.arguments, status=status, summary=summary)
        turn_tools.append({"name": tc.name, "args": tc.arguments, "status": status, "ms": ms})
        # 身份核验钩子：lookup_user 精确命中即视为核验通过（demo 级简化，见 README）
        if status == "ok" and tc.name == "lookup_user" and isinstance(data, dict):
            self._maybe_verify_identity(data, state)
        return rec, result

    def _maybe_verify_identity(self, data: Dict[str, Any], state: SessionState) -> None:
        users = data.get("users", [])
        if data.get("match_count") == 1 and users:
            u = users[0]
            state.user = u
            state.issue.user_id = u.get("employee_id", state.issue.user_id)
            if not state.issue.verified_identity:
                state.issue.verified_identity = True
                state.issue.description = state.issue.description or "（待补充）"

    def _reconcile_actions(self, final: Dict[str, Any], state: SessionState) -> Dict[str, Any]:
        """动作对账：final 里声明的动作必须真实执行过，否则现在补执行。

        - 只读诊断工具（search_kb/lookup_user 等）不是"动作"：LLM 偶尔会把它们
          误声明进 actions_taken，这里直接跳过，绝不能拿去 execute_action（会被
          策略拒掉造成误升级）
        - 已成功执行过 → 跳过（防止重复执行）
        - 未执行 → 用 execute_action 确定性地补执行（内部有策略硬校验）
        - 补执行被拒 → 强制升级（身份未核验 → identity_unverified，其余 → policy_forbidden）
        """
        declared = [a for a in (final.get("actions_taken") or []) if isinstance(a, dict)]
        declared = [
            a for a in declared if a.get("action") not in READONLY_TOOLS
        ]
        if not declared:
            return final
        executed_ok = {
            (r.args or {}).get("action")
            for r in state.tool_records
            if r.name == "execute_action" and r.status == "ok"
        }
        denied_clause = ""
        denied_action = ""
        for act in declared:
            name = act.get("action", "")
            if not name or name in executed_ok:
                continue
            tc = ToolCall(id=f"reconcile-{name}", name="execute_action",
                          arguments={"action": name, "params": act.get("params", {})})
            result = self.registry.safe_execute(tc, ToolContext(settings=self.settings, state=state))
            data = result.get("data", {})
            ok = result.get("status") == "ok" and isinstance(data, dict) and data.get("status") == "ok"
            state.tool_records.append(ToolRecord(
                turn=state.turn_count, name="execute_action",
                args=tc.arguments, status="ok" if ok else "error",
                summary=f"对账补执行 {name}: {'成功' if ok else data.get('policy_clause', '失败')}",
            ))
            if not ok:
                clause = str(data.get("policy_clause", "")) if isinstance(data, dict) else ""
                denied_clause = denied_clause or clause
                denied_action = denied_action or name
        if denied_clause:
            reason = (
                "identity_unverified" if "identity" in denied_clause.lower() else "policy_forbidden"
            )
            esc = dict(final)
            esc["state"] = "escalated"
            esc["escalation"] = dict(esc.get("escalation") or {})
            esc["escalation"]["reason"] = reason
            esc["escalation"]["forbidden_action"] = denied_action
            esc["user_message"] = (
                "这个操作我暂时无法完成（" + denied_clause.split(":")[-1] + "），"
                "已转人工处理。"
            )
            return self.outcome.complete_summary(esc, state, force_reason=reason)
        return final

    def _build_turn_result(
        self, state: SessionState, final: Dict[str, Any],
        turn_tools: List[Dict[str, Any]], latency_ms: int,
    ) -> TurnResult:
        esc = final.get("escalation") if final.get("state") == "escalated" else None
        if esc and state.escalation_ticket:
            esc = {**esc, "ticket_id": state.escalation_ticket["id"]}
        usage = {"prompt": state.total_usage["prompt"], "completion": state.total_usage["completion"]}
        return TurnResult(
            session_id=state.session_id,
            reply=final.get("user_message", ""),
            state=final.get("state", "need_info"),
            confidence=float(final.get("confidence", 0.0) or 0.0),
            evidence=[e for e in final.get("evidence", []) if isinstance(e, str)],
            tools_used=turn_tools,
            escalation=esc,
            latency_ms=latency_ms,
            usage=usage,
        )

    def _closed_turn(self, state: SessionState, t0: float) -> TurnResult:
        if state.escalated:
            reply = (
                f"该问题已升级人工处理（工单 {state.escalation_ticket['id']}），"
                "IT 同事会尽快联系你。如需补充信息可直接回复本会话。"
            )
            st = "escalated"
        else:
            reply = "该问题已解决并关闭。如果还有其他 IT 问题，随时告诉我！"
            st = "resolved"
        return TurnResult(
            session_id=state.session_id, reply=reply, state=st, confidence=1.0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            usage=state.total_usage.copy(),
        )

    def _graceful_failure(self, state: SessionState, err: str, t0: float) -> TurnResult:
        return TurnResult(
            session_id=state.session_id,
            reply="抱歉，系统暂时遇到一点技术问题，请稍后重试。若紧急请直接联系 IT 热线。",
            state="need_info",
            confidence=0.0,
            latency_ms=int((time.monotonic() - t0) * 1000),
            usage=state.total_usage.copy(),
        )


def parse_final_json(content: Optional[str]) -> Optional[Dict[str, Any]]:
    """宽松解析最终 JSON：剥代码围栏 → 提取首个 {...} → json.loads。"""
    if not content or "{" not in content:
        return None
    text = content.strip()
    if "```" in text:  # 剥 ```json 围栏
        parts = text.split("```")
        for part in parts:
            p = part.strip()
            if p.startswith("json"):
                p = p[4:]
            if p.lstrip().startswith("{"):
                text = p.strip()
                break
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start: end + 1])
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None
