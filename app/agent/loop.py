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

from app.agent.outcome import (
    READONLY_TOOLS, OutcomeEngine, force_escalation_final, normalize_final,
)
from app.agent.prompts import build_messages, build_system_prompt, summarize_tool_result
from app.agent.state import IssueContext, SessionState, ToolRecord, new_session_id
from app.llm.base import BaseLLM, ToolCall
from app.tools.policy import PolicyEngine
from app.tools.registry import ToolContext, ToolRegistry

MAX_CONSECUTIVE_TOOL_ERRORS = 2

# 已知部门（与 user_directory.json 的 department 字段对齐）+ 常见中文别名。
# 用于从用户消息中提取部门声明，支持在 lookup_user 后立即核验身份，
# 避免 LLM 同一轮内先查目录再执行动作时因 verified_identity 未置位而被拒。
_DEPARTMENT_ALIASES = {
    "engineering": ["engineering", "工程", "研发", "技术部", "研发部"],
    "finance": ["finance", "财务", "财务部"],
    "sales": ["sales", "销售", "销售部"],
    "marketing": ["marketing", "市场", "市场部", "营销"],
    "data": ["data", "数据", "数据部"],
    "hr": ["hr", "人力资源", "人事", "人事部"],
    "it": ["it", "信息技术", "it部"],
    "operations": ["operations", "运营", "运营部"],
    "executive_office": ["executive_office", "高管办", "高管", "ceo办公室"],
}


def _extract_department(text: str) -> str:
    """从文本中提取部门声明，返回目录标准部门名（小写英文）；找不到返回空串。

    处理否定语境：若部门名出现在"不是X"/"并非X"/"非X"/"not X"/"no X"
    等否定结构中，或"X不是"结构中，则该部门不计入，只取肯定声明的部门。
    例如"我的部门是sales，不是engineering"应返回 sales，而非 engineering。
    """
    if not text:
        return ""
    import re as _re

    # 收集所有别名，按长度降序排列避免短别名先匹配
    all_aliases = []
    for canonical, aliases in _DEPARTMENT_ALIASES.items():
        for alias in aliases:
            all_aliases.append((canonical, alias))
    all_aliases.sort(key=lambda x: len(x[1]), reverse=True)

    # 第一步：移除"否定词 + 部门名"结构（不是X / 并非X / 非X / not X / no X / non-X）
    cleaned = text
    for _canonical, alias in all_aliases:
        pattern = _re.compile(
            r"(?:不是|并非|非|not\s+|no\s+|non-)\s*" + _re.escape(alias),
            _re.IGNORECASE,
        )
        cleaned = pattern.sub("", cleaned)
    # 第二步：移除"部门名 + 否定词"结构（X不是 / X并非 / X非）
    for _canonical, alias in all_aliases:
        pattern = _re.compile(
            _re.escape(alias) + r"\s*(?:不是|并非|非)",
            _re.IGNORECASE,
        )
        cleaned = pattern.sub("", cleaned)

    # 从清理后的文本中提取第一个出现的部门别名（肯定声明）
    lower = cleaned.lower()
    for canonical, aliases in _DEPARTMENT_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lower:
                return canonical
    return ""


def _extract_employee_id(text: str) -> str:
    """从文本中提取工号（E 开头加数字），找不到返回空串。"""
    if not text:
        return ""
    import re as _re
    m = _re.search(r"[Ee]\d{3,}", text)
    return m.group(0).upper() if m else ""


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

        # 问题结束 ≠ 会话结束：
        # - resolved 后新消息 → 视为新问题，但保留未解决的症状/疑点（S02：
        #   一个问题解决后另一个未解决问题的状态不丢失），身份与历史也保留
        # - escalated 后新消息 → 作为补充信息追加到工单，不丢弃
        if state.resolved and not state.escalated:
            old_issue = state.issue
            state.resolved = False
            state.issue = IssueContext()  # 新问题卡，保留 state.user（已核验身份）
            # S02：继承上一问题卡中未解决的症状和疑点，避免 VPN 等并行问题状态丢失
            state.issue.symptoms = list(old_issue.symptoms)
            state.issue.unresolved_questions = [
                q for q in old_issue.unresolved_questions if q.status == "open"
            ]
            state.issue.conflicts = [c for c in old_issue.conflicts if c.status == "active"]
            state.hypotheses = []
            state.consecutive_need_info = 0
        if state.escalated:
            # 升级后仍允许补充信息：记录到工单并回复确认，不进入诊断循环
            state.append_message("user", user_message)
            if state.escalation_ticket:
                state.escalation_ticket.setdefault("supplementary", []).append(user_message)
            reply = (
                f"已收到你的补充信息，已追加到工单 {state.escalation_ticket['id']}。"
                "IT 同事会结合这些信息继续处理。"
            )
            return TurnResult(
                session_id=state.session_id, reply=reply, state="escalated",
                confidence=1.0, latency_ms=int((time.monotonic() - t0) * 1000),
                usage=state.total_usage.copy(),
            )

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

            # 分支 2：最终答案 → 解析 JSON 契约 → 契约类型归一化
            final = parse_final_json(resp.content)
            if final is not None:
                # F05.2：合法 JSON 但字段类型错误（evidence=42 等）必须归一化，
                # 防止后续遍历 evidence/actions_taken 时 TypeError 崩溃。
                # state 非法时 normalize_final 返回 None，走下方有界重试路径。
                final = normalize_final(final)
            if final is None:
                if not state.final_retry_used:
                    state.final_retry_used = True
                    state.append_message(
                        "system",
                        "【系统提示】你的上一条输出不是合法的契约 JSON，"
                        "或字段类型不符合契约要求（如 confidence 须为数值、"
                        "evidence/actions_taken 须为列表）。"
                        "请严格按输出契约重新作答，只输出一个 JSON 对象。",
                    )
                    continue
                final = self.outcome.fallback_final(state, "parse_failed_after_retry")
                state.append_message("assistant", resp.content or "")
            break

        if final is None:  # 步数耗尽 → 强制升级（摘要由状态卡合成，不依赖 LLM）
            final = force_escalation_final(state, "max_steps_exceeded")

        # 先合并本轮描述性事实，再做身份核验（部门只取用户消息），最后对账执行动作。
        state.issue.merge(final.get("issue_update", {}))
        # F04：用户直接观察消解已知冲突时自动标记 resolved，
        # 避免 LLM 忘记在 issue_update.resolved_conflicts 中声明而导致冲突永久 active。
        self._auto_resolve_conflicts(state, self._latest_user_message(state))
        self._verify_identity(state)
        final = self._retry_identity_denied_actions(state, final)  # 重试因身份被拒的动作
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
        state.apply_final_state_flags(final)  # 只设 escalated/resolved/hypotheses，不重复 merge issue
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

    def _verify_identity(self, state: SessionState) -> None:
        """LLM 循环后的身份核验（与 _try_early_verify 共用统一入口）。

        在 issue_update 合并后调用，确保 state.user（lookup_user 命中的目录记录）
        已就绪。核验逻辑与早期核验完全一致，避免双份判断漂移。
        """
        self._check_identity_claim(state)

    @staticmethod
    def _latest_user_message(state: SessionState) -> str:
        """反向查找最近一条用户消息的正文（工具循环中 messages 末尾可能是 tool 结果）。"""
        for m in reversed(state.messages):
            if m.get("role") == "user":
                return m.get("content", "") or ""
        return ""

    def _check_identity_claim(self, state: SessionState) -> None:
        """统一身份核验入口：_try_early_verify（工具执行中）与 _verify_identity（循环后）共用。

        核验规则（双字段必须同时匹配，缺一不可）：
        1. 工号匹配：用户消息中声明的工号必须与目录记录的 employee_id 完全一致。
           部门相同但工号不同（如查了 E3001 但用户自称 E1001）不通过（B01）。
        2. 部门匹配：用户消息中肯定声明的部门必须与目录记录的 department 一致。
           否定声明（"不是 engineering"）不计入匹配（B02）。
        3. 多候选不核验：lookup_user 返回多条命中时无法确定目标，不核验。
        4. 歧义不核验：用户未明确声明工号或部门时不核验，继续澄清。

        安全约束：部门声明只信用户消息，绝不读取 issue.expected_department（LLM 可写）。
        核验过程记录到 state.identity_claim，便于追溯依据。
        """
        if state.issue.verified_identity:
            return
        u = state.user
        if not u:
            return
        user_msg = self._latest_user_message(state)
        claimed_eid = _extract_employee_id(user_msg)
        claimed_dept = _extract_department(user_msg)
        dir_eid = (u.get("employee_id") or "").strip()
        dir_dept = (u.get("department") or "").strip().lower()

        claim = {
            "source": "user_message",
            "claimed_employee_id": claimed_eid,
            "claimed_department": claimed_dept,
            "directory_employee_id": dir_eid,
            "directory_department": dir_dept,
            "eid_match": bool(claimed_eid) and claimed_eid == dir_eid,
            "dept_match": bool(claimed_dept) and claimed_dept == dir_dept,
        }
        state.identity_claim = claim

        # 双字段必须同时匹配：工号一致且部门一致才核验通过
        if claim["eid_match"] and claim["dept_match"]:
            state.mark_identity_verified(u)
            claim["result"] = "verified"
        else:
            claim["result"] = "unverified"
            reason = []
            if not claimed_eid:
                reason.append("未声明工号")
            elif not claim["eid_match"]:
                reason.append(f"工号不匹配(声明{claimed_eid}≠目录{dir_eid})")
            if not claimed_dept:
                reason.append("未声明部门")
            elif not claim["dept_match"]:
                reason.append(f"部门不匹配(声明{claimed_dept}≠目录{dir_dept})")
            claim["reason"] = "；".join(reason)

    @staticmethod
    def _auto_resolve_conflicts(state: SessionState, user_message: str) -> None:
        """用户直接观察消解已知冲突时，自动标记为 resolved。

        F04：LLM 有时忘记在 issue_update.resolved_conflicts 中声明，导致冲突
        永久 active，触发 outcome 规则 0 反复改写 need_info。此处作为安全网：
        若用户消息提到了冲突中的关键实体（如 SH-01/HZ-01）且包含确认/澄清用语，
        则认为该冲突已被用户直接观察消解。
        """
        import re as _re
        if not user_message or not state.issue.conflicts:
            return
        msg_lower = user_message.lower()
        # 确认/澄清用语：用户给出了直接观察而非继续提问
        resolution_words = [
            "实际", "现在", "当前", "确认", "选中", "连的是", "连接的是",
            "用的是", "选的是", "不是", "没有", "并非", "actually", "currently",
        ]
        has_resolution = any(w in user_message for w in resolution_words)
        if not has_resolution:
            return
        for c in state.issue.conflicts:
            if c.status != "active":
                continue
            # 提取冲突中的关键实体（大写字母+数字组合，如 SH-01、HZ-01、CHG-339）
            entities = _re.findall(r"[A-Z]{1,5}-?\d{1,4}", c.text)
            if not entities:
                continue
            mentioned = sum(1 for e in entities if e.lower() in msg_lower)
            # 用户消息提到至少一半关键实体 + 有确认用语 → 消解
            if mentioned >= max(1, len(entities) // 2):
                c.status = "resolved"

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
        """执行单个工具，返回 (ToolRecord, 回填给 LLM 的结果)。

        对 execute_action 走统一入口 _run_action：ToolRecord.status 反映业务结果
        （ok=动作成功 / error=denied 或错误），不能只看外层 transport 的 ok；
        成功时把真实 data 存入 ToolRecord，供回核对账生成回复。
        """
        t0 = time.monotonic()
        if tc.name == "execute_action":
            action = (tc.arguments or {}).get("action", "")
            params = (tc.arguments or {}).get("params") or {}
            result, biz_ok, data, clause = self._run_action(action, params, state, call_id=tc.id)
            status = "ok" if biz_ok else "error"
        else:
            result = self.registry.safe_execute(tc, ToolContext(settings=self.settings, state=state))
            transport_status = result.get("status", "error")
            data = result.get("data", result)
            status = transport_status
        ms = int((time.monotonic() - t0) * 1000)

        summary = (
            summarize_tool_result(tc.name, data)
            if status == "ok" and isinstance(data, dict)
            else str(result.get("error", (data or {}).get("policy_clause", "")))[:120]
        )
        rec_data = data if (tc.name == "execute_action" and status == "ok") else None
        rec = ToolRecord(turn=state.turn_count, name=tc.name, args=tc.arguments,
                         status=status, summary=summary, data=rec_data)
        turn_tools.append({"name": tc.name, "args": tc.arguments, "status": status, "ms": ms})
        # lookup_user 命中时暂存用户目录上下文（角色/部门等），但不立即核验；
        # 真正的核验在 apply_final 后基于"用户消息中的部门"执行
        # （部门声明只信用户消息，不信 LLM 写的 expected_department）
        if result.get("status") == "ok" and tc.name == "lookup_user" and isinstance(data, dict):
            self._cache_user_directory(data, state)
        return rec, result

    def _cache_user_directory(self, data: Dict[str, Any], state: SessionState) -> None:
        """暂存 lookup_user 返回的目录记录，供后续身份核验。

        安全要点：
        1) state.user 只是"最近一次查询结果"，不是可信身份；可信身份在 verified_user。
        2) 身份切换检测：若已核验主体与新查询的工号不同，重置核验状态 ——
           不能把 E1001 的认证转移给 E1002。必须在覆盖 state.user 之前读取旧主体。
        3) 早期核验：若用户消息里工号+部门都明确且与目录一致，立即核验通过，
           使同一轮内后续 execute_action 能通过身份校验。
        4) 多候选不核验：match_count > 1 时无法确定目标，只暂存首条供展示，不核验。
        """
        users = data.get("users", [])
        match_count = data.get("match_count", 0)
        if match_count == 1 and users:
            u = users[0]
            # 覆盖 state.user 前先记录已核验主体（防止读到切换后的 user）
            old_subject = state.verified_user
            if old_subject is None and state.issue.verified_identity:
                old_subject = state.user  # 旧版/测试直接置位的兼容路径
            # 身份切换：新查询工号与已核验主体不一致 → 必须重新核验
            if old_subject and old_subject.get("employee_id") != u.get("employee_id"):
                state.reset_identity_verification()
            state.user = u  # 仅更新查询上下文，不代表核验通过
            state.issue.user_id = u.get("employee_id", state.issue.user_id)
            self._check_identity_claim(state)  # 早期核验：与循环后共用同一入口
        elif match_count > 1:
            # 多候选：暂存首条记录供展示，但不核验（身份不明）
            if users:
                state.user = users[0]
            state.identity_claim = {
                "source": "user_message",
                "result": "ambiguous",
                "reason": f"lookup_user 返回 {match_count} 条命中，无法确定身份",
            }
        elif match_count == 0:
            # 查无此人：仅当用户在消息中明确提供了工号（E\d+）时，才标记身份核验失败。
            # 若 LLM 自行对无关查询（如 VPN 场景的模糊查询）调 lookup_user 返回 0，
            # 不应把升级原因带偏为 identity_unverified。
            if _extract_employee_id(self._latest_user_message(state)):
                state.identity_lookup_no_match = True

    # ---- 统一动作执行（原生调用 / 重试 / 补执行共用入口）----

    def _run_action(
        self, action: str, params: Dict[str, Any], state: SessionState, call_id: str = ""
    ):
        """统一动作执行入口。

        执行前由 actions.py 完成需求(白名单)、身份、目标绑定、资源白名单校验。
        返回 (full_result, biz_ok, data, clause)：
        - full_result: registry 原始返回（供回填消息历史）
        - biz_ok: 业务是否成功（transport ok 且 data.status == ok）
        - data: 动作 data 部分
        - clause: 失败原因（策略条款或传输错误）
        """
        tc = ToolCall(
            id=call_id or f"act-{action}", name="execute_action",
            arguments={"action": action, "params": params},
        )
        result = self.registry.safe_execute(tc, ToolContext(settings=self.settings, state=state))
        transport_ok = result.get("status") == "ok"
        data = result.get("data", result) if transport_ok else {}
        if not isinstance(data, dict):
            data = {}
        biz_ok = transport_ok and data.get("status") == "ok"
        clause = ""
        if not biz_ok:
            clause = str(data.get("policy_clause", ""))
            if not clause and not transport_ok:
                clause = str(result.get("error", "transport_failure"))
        return result, biz_ok, data, clause

    @staticmethod
    def _action_dedup_key(
        action: str, params: Dict[str, Any], state: SessionState
    ) -> tuple:
        """幂等键 = (动作, 目标, 资源签名)。

        资源签名捕获区分不同资源的参数（如 grant_standard_software 的 software），
        避免 jira 与 grafana 被错误去重为同一动作（修复 A03）。
        """
        p = params or {}
        target = p.get("employee_id") or (state.verified_subject or {}).get("employee_id", "")
        if action == "grant_standard_software":
            resource = str(p.get("software", "")).strip().lower()
        else:
            resource = ""
        return (action, target, resource)

    @staticmethod
    def _build_reply_from_results(results: List[Dict[str, Any]]) -> str:
        """依据真实执行结果重新生成回复（禁止仅修改状态或追加密码）。

        按动作类型生成确定性话术，保证多轮/多次运行一致性（E01 三次话术一致）。
        results: 成功执行的动作 data 列表。
        """
        parts: List[str] = []
        for r in results:
            if not isinstance(r, dict):
                continue
            action = r.get("action", "")
            target = r.get("target", "")
            res = r.get("result", {}) or {}
            if action == "reset_password":
                pw = res.get("temporary_password", "")
                expires = res.get("expires_in_minutes", 30)
                parts.append(
                    f"身份核验通过，已为你重置密码。临时密码：{pw}，"
                    f"{expires} 分钟内有效，首次登录请立即修改。"
                )
            elif action == "unlock_account":
                parts.append(f"已为你解锁账号 {target}，账号状态已恢复为 active。")
            elif action == "grant_standard_software":
                sw = res.get("granted", "")
                sync = res.get("sync_minutes", 5)
                parts.append(f"已为你授予 {sw} 权限，约 {sync} 分钟后同步生效。")
        return "".join(parts)

    def _find_executed_action_data(
        self, state: SessionState, action: str, params: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """从 tool_records 中查找已成功执行的同键动作的真实 data（供回复生成使用）。"""
        key = self._action_dedup_key(action, params, state)
        for r in state.tool_records:
            if r.name != "execute_action" or r.status != "ok":
                continue
            rkey = self._action_dedup_key(
                (r.args or {}).get("action", ""), (r.args or {}).get("params") or {}, state
            )
            if rkey == key:
                return r.data
        return None

    def _retry_identity_denied_actions(
        self, state: SessionState, final: Dict[str, Any]
    ) -> Dict[str, Any]:
        """身份核验通过后，重试本轮内因"身份未核验"被拒的敏感动作。

        场景：LLM 同一轮内先 lookup_user 再 execute_action，但 execute_action
        执行时 verified_identity 尚未置位（早期核验未命中）。现在核验通过了，
        重试这些动作；成功则把真实结果存入 ToolRecord.data 供回复生成。
        """
        if not state.issue.verified_identity:
            return final
        for rec in state.tool_records:
            if rec.turn != state.turn_count:
                continue
            if rec.name != "execute_action" or rec.status != "error":
                continue
            args = rec.args or {}
            action = args.get("action", "")
            if action not in ("reset_password", "unlock_account", "grant_standard_software"):
                continue
            if "identity" not in (rec.summary or "").lower():
                continue
            params = args.get("params") or {}
            _, biz_ok, data, clause = self._run_action(action, params, state, call_id=f"retry-{action}")
            rec.status = "ok" if biz_ok else "error"
            rec.summary = f"身份核验后重试 {action}: {'成功' if biz_ok else clause}"
            rec.data = data if biz_ok else None
            if biz_ok:
                final = dict(final)
                final["state"] = "resolved"
                try:
                    final["confidence"] = max(float(final.get("confidence") or 0.5), 0.9)
                except (TypeError, ValueError):
                    final["confidence"] = 0.9
                actions = list(final.get("actions_taken") or [])
                if not any(
                    isinstance(a, dict) and a.get("action") == action
                    and (a.get("params") or {}).get("software") == params.get("software")
                    for a in actions
                ):
                    actions.append({"action": action, "params": params})
                    final["actions_taken"] = actions
        return final

    def _reconcile_actions(self, final: Dict[str, Any], state: SessionState) -> Dict[str, Any]:
        """动作对账：final 里声明的动作必须真实执行过，否则现在补执行。

        - 只读诊断工具不是"动作"，跳过
        - 幂等键 = (动作, 目标, 资源签名)，含完整资源参数，避免不同资源被错误去重（A03）
        - 补执行被拒或传输失败 → 强制升级（不能声称成功，修复 A04）
        - 全部成功 → 依据真实结果重新生成回复，不保留 LLM 可能编造的密码/状态
        """
        declared = [a for a in (final.get("actions_taken") or []) if isinstance(a, dict)]
        declared = [a for a in declared if a.get("action") not in READONLY_TOOLS]
        if not declared:
            return final

        # 已成功执行的动作集合（按完整资源去重键）
        executed_ok = set()
        for r in state.tool_records:
            if r.name == "execute_action" and r.status == "ok":
                key = self._action_dedup_key(
                    (r.args or {}).get("action", ""), (r.args or {}).get("params") or {}, state
                )
                if key[0]:
                    executed_ok.add(key)

        denied_clause = ""
        denied_action = ""
        ok_results: List[Dict[str, Any]] = []
        for act in declared:
            name = act.get("action", "")
            if not name:
                continue
            params = act.get("params") or {}
            key = self._action_dedup_key(name, params, state)
            if key in executed_ok:
                # 已成功执行过，收集真实结果用于生成回复
                data = self._find_executed_action_data(state, name, params)
                if data:
                    ok_results.append(data)
                continue
            _, biz_ok, data, clause = self._run_action(name, params, state, call_id=f"reconcile-{name}")
            state.tool_records.append(ToolRecord(
                turn=state.turn_count, name="execute_action",
                args={"action": name, "params": params},
                status="ok" if biz_ok else "error",
                summary=f"对账补执行 {name}@{key[1]}: {'成功' if biz_ok else clause}",
                data=data if biz_ok else None,
            ))
            if biz_ok:
                executed_ok.add(key)
                ok_results.append(data)
            else:
                denied_clause = denied_clause or clause
                denied_action = denied_action or name

        # 任意动作失败（含传输故障）→ 强制升级，绝不声称成功
        if denied_clause:
            reason = (
                "identity_unverified" if "identity" in denied_clause.lower()
                else "policy_forbidden" if any(k in denied_clause.lower() for k in ("policy", "condition", "whitelist", "forbidden"))
                else "action_failed"
            )
            esc = dict(final)
            esc["state"] = "escalated"
            esc["escalation"] = dict(esc.get("escalation") or {})
            esc["escalation"]["reason"] = reason
            esc["escalation"]["forbidden_action"] = denied_action
            detail = denied_clause.split(":")[-1] if ":" in denied_clause else denied_clause
            esc["user_message"] = (
                f"这个操作我暂时无法完成（{detail}），已转人工处理。"
            )
            return self.outcome.complete_summary(esc, state, force_reason=reason)

        # 全部成功：依据真实结果重新生成回复（禁止仅追加密码）
        if ok_results:
            reply = self._build_reply_from_results(ok_results)
            if reply:
                final = dict(final)
                final["user_message"] = reply
                final["state"] = "resolved"
                final["confidence"] = 0.9
        return final

    def _build_turn_result(
        self, state: SessionState, final: Dict[str, Any],
        turn_tools: List[Dict[str, Any]], latency_ms: int,
    ) -> TurnResult:
        esc = final.get("escalation") if final.get("state") == "escalated" else None
        if esc and state.escalation_ticket:
            esc = {**esc, "ticket_id": state.escalation_ticket["id"]}
        # 安全转换：LLM 可能返回非数值 confidence（如 "abc"），不能因此崩溃
        conf = final.get("confidence", 0.0)
        try:
            conf = float(conf) if conf is not None else 0.0
        except (TypeError, ValueError):
            conf = 0.0
        usage = {"prompt": state.total_usage["prompt"], "completion": state.total_usage["completion"]}
        return TurnResult(
            session_id=state.session_id,
            reply=final.get("user_message", ""),
            state=final.get("state", "need_info"),
            confidence=conf,
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
        """LLM 调用故障的优雅降级：友好回复 + 保留状态 + 记录故障到 trace。

        F06：模型网络/限流/配额故障后，必须：
        1. 不崩溃、不丢失会话状态（用户可重试）
        2. 把故障本身记录进 tool_records 和 trace，便于事后定位
        """
        # 把 LLM 故障记录为一条 error 状态的工具记录，保证 trace 中有据可查
        state.tool_records.append(ToolRecord(
            turn=state.turn_count,
            name="llm_call",
            args={},
            status="error",
            summary=err[:200],
        ))
        final = {
            "state": "need_info",
            "confidence": 0.0,
            "user_message": "抱歉，系统暂时遇到一点技术问题，请稍后重试。若紧急请直接联系 IT 热线。",
            "issue_update": {},
            "actions_taken": [],
            "evidence": [f"llm_failure: {err[:120]}"],
        }
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = TurnResult(
            session_id=state.session_id,
            reply=final["user_message"],
            state="need_info",
            confidence=0.0,
            latency_ms=latency_ms,
            usage=state.total_usage.copy(),
            evidence=final["evidence"],
            tools_used=[{"name": "llm_call", "args": {}, "status": "error", "ms": latency_ms}],
        )
        # F06：故障轮次也必须写入 trace，不能因为走降级分支就跳过日志
        if self.tracer:
            from app.agent.outcome import Verdict
            self.tracer.log_turn(state, final, Verdict("pass", final, reason="llm_failure"), result)
        return result


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
