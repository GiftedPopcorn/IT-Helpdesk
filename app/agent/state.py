"""会话状态：双轨记忆。

- 原始消息历史（messages）：OpenAI 格式，隐式记忆，构建提示时只取最近 N 条
- 结构化问题卡（IssueContext）+ 假设列表 + 工具记录：显式记忆，
  每轮注入系统提示，使第 15 轮的输入长度仍然稳定（成本/延迟可控）

显式状态是"跨轮次追踪已调查内容和未解决问题"的直接实现，
也是升级摘要的数据来源 —— 摘要不依赖 LLM 回忆，从状态卡合成。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class IssueContext:
    """结构化问题卡（LLM 每轮通过 final.issue_update 维护）。

    Step 2：每个问题带稳定 issue_id，跨轮保留；同一类别可存在多个问题。
    status / completion_basis 由代码根据真实回执和完成依据写入，LLM 不直接置位。
    """

    issue_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    category: str = "unknown"  # password | vpn | access | account | other
    description: str = ""
    goal: str = ""  # 该问题期望达成的目标（如"重置 E1001 密码"）
    user_id: str = ""
    # 身份核验状态：仅由代码在目录双字段比对通过后置位，LLM 无法通过 issue_update 提升
    verified_identity: bool = False
    # 用户声明的部门（LLM 可写入作为待核验事实的记录，但实际核验只采用用户消息中的部门，
    # 不会读取此字段 —— 防止模型把 expected_department 填成目录部门来绕过核验）
    expected_department: str = ""
    affected_service: str = ""
    symptoms: List[str] = field(default_factory=list)
    attempted: List[str] = field(default_factory=list)
    unresolved_questions: List[Question] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)  # 数据源矛盾点
    # 由代码写入的问题状态与完成依据（LLM 不直接置位）
    status: str = "open"  # open | resolved | pending_user | escalated
    completion_basis: str = ""  # action_executed | user_confirmed | answer_delivered | ""

    def merge(self, update: Dict[str, Any]) -> None:
        """用 LLM 给的增量更新合并问题卡（None/缺省字段不覆盖）。

        conflicts / unresolved_questions 支持两种输入格式（向后兼容）：
        - 字符串列表：["kb: A vs system: B"] → 自动包装为带 ID 的对象
        - 对象列表：[{"text": "...", "status": "active", "evidence": [...]}] → 直接使用
        resolved_conflicts 支持按 text 或 id 匹配并关闭对应冲突。
        """
        if not isinstance(update, dict):
            return
        for key in ("category", "description", "goal", "user_id", "affected_service", "expected_department"):
            v = update.get(key)
            if isinstance(v, str) and v:
                setattr(self, key, v)
        # 显式忽略 LLM 对 verified_identity / issue_id / status / completion_basis 的声明
        # （这些是安全/状态字段，仅代码可写）
        for key in ("symptoms", "attempted"):
            v = update.get(key)
            if isinstance(v, list):
                merged = getattr(self, key)
                for item in v:
                    if isinstance(item, str) and item and item not in merged:
                        merged.append(item)
        # 冲突 / 疑点：带稳定 ID，支持按 text 去重与按 status 关闭
        self._merge_conflicts(update.get("conflicts"), update.get("resolved_conflicts"))
        self._merge_questions(update.get("unresolved_questions"), update.get("answered_questions"))

    def _merge_conflicts(
        self, new_conflicts: Any, resolved: Any
    ) -> None:
        """合并冲突：按 text 去重；resolved_conflicts 中的项（按 text 或 id）关闭为 resolved。"""
        if isinstance(new_conflicts, list):
            for item in new_conflicts:
                text = item if isinstance(item, str) else (item.get("text") if isinstance(item, dict) else "")
                if not text:
                    continue
                existing = next((c for c in self.conflicts if c.text == text), None)
                if existing:
                    if isinstance(item, dict):
                        existing.status = item.get("status", existing.status)
                        for ev in item.get("evidence", []) or []:
                            if isinstance(ev, str) and ev not in existing.evidence:
                                existing.evidence.append(ev)
                else:
                    ev = item.get("evidence", []) if isinstance(item, dict) else []
                    self.conflicts.append(Conflict(
                        text=text,
                        status=item.get("status", "active") if isinstance(item, dict) else "active",
                        evidence=[e for e in ev if isinstance(e, str)],
                    ))
        # 关闭已解决的冲突（支持按 text 或 id 匹配）
        if isinstance(resolved, list):
            for item in resolved:
                key = item if isinstance(item, str) else (item.get("id") or item.get("text") if isinstance(item, dict) else "")
                if not key:
                    continue
                for c in self.conflicts:
                    if c.text == key or c.id == key:
                        c.status = "resolved"
        # 保留已 resolved 的冲突供历史追溯（F04 质量要求：历史冲突可追溯）。
        # active_conflicts 属性已按 status 过滤，不会影响结果审核规则 0。
        # 上限防护：resolved 冲突最多保留 20 条，防止极端长会话无限增长。
        active = [c for c in self.conflicts if c.status == "active"]
        resolved = [c for c in self.conflicts if c.status == "resolved"][-20:]
        self.conflicts = active + resolved

    def _merge_questions(
        self, new_questions: Any, answered: Any
    ) -> None:
        """合并疑点：按 text 去重；answered_questions 中的项关闭为 answered。"""
        if isinstance(new_questions, list):
            for item in new_questions:
                text = item if isinstance(item, str) else (item.get("text") if isinstance(item, dict) else "")
                if not text:
                    continue
                existing = next((q for q in self.unresolved_questions if q.text == text), None)
                if existing:
                    if isinstance(item, dict):
                        existing.status = item.get("status", existing.status)
                else:
                    self.unresolved_questions.append(Question(
                        text=text,
                        status=item.get("status", "open") if isinstance(item, dict) else "open",
                    ))
        if isinstance(answered, list):
            for item in answered:
                key = item if isinstance(item, str) else (item.get("id") or item.get("text") if isinstance(item, dict) else "")
                if not key:
                    continue
                for q in self.unresolved_questions:
                    if q.text == key or q.id == key:
                        q.status = "answered"
        # 移除已回答的疑点，避免永久累加
        self.unresolved_questions = [q for q in self.unresolved_questions if q.status != "answered"]

    @property
    def active_conflicts(self) -> List[str]:
        """当前活跃冲突的文本列表（供 outcome 规则判断用）。"""
        return [c.text for c in self.conflicts if c.status == "active"]

    def to_prompt(self) -> str:
        verified = "是" if self.verified_identity else "否"
        conflicts_str = "; ".join(
            f"[{c.status}] {c.text}" for c in self.conflicts
        ) or "无"
        questions_str = "; ".join(
            f"[{q.status}] {q.text}" for q in self.unresolved_questions
        ) or "无"
        return (
            f"类别={self.category} | 描述={self.description or '（待补充）'} | "
            f"用户={self.user_id or '未知'} | 身份核验={verified} | "
            f"用户声明部门={self.expected_department or '未提供'} | "
            f"影响服务={self.affected_service or '未知'} | "
            f"症状={self.symptoms or '无'} | 已尝试={self.attempted or '无'} | "
            f"未解决疑点={questions_str} | "
            f"证据冲突={conflicts_str}"
        )


@dataclass
class Hypothesis:
    """一条诊断假设及其验证状态（带稳定 ID，支持跨轮更新/关闭）。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str = ""
    status: str = "testing"  # testing | confirmed | rejected | conflicted
    evidence: List[str] = field(default_factory=list)


@dataclass
class Conflict:
    """一条证据冲突（带稳定 ID + 状态，支持更新与关闭，避免列表永久累加）。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str = ""
    status: str = "active"  # active | resolved
    evidence: List[str] = field(default_factory=list)


@dataclass
class Question:
    """一条未解决疑点（带稳定 ID + 状态，已回答的可关闭不再追问）。"""

    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    text: str = ""
    status: str = "open"  # open | answered
    evidence: List[str] = field(default_factory=list)


@dataclass
class DepartmentClaim:
    """本人部门声明（Step 2：只有明确归属于当前用户的声明才会被保存）。

    - value: 目录标准部门名（小写英文）
    - subject: 声明主体，目前只接受 "self"；"other" 直接丢弃
    - source: 声明来源：explicit_self（本人明确声明）/ identity_answer（回答系统身份问题）
    - source_turn: 声明所在轮次
    - evidence_text: 触发匹配的原始子句（如 "我的部门是 engineering"）
    - identity_epoch: 所属身份上下文编号（本人工号变更时递增，旧 epoch 声明失效）

    安全约束：subject / source 由代码验证后写入，模型不能直接设置"已接受/已核验"。
    """

    value: str = ""
    subject: str = ""  # self | other
    source: str = ""  # explicit_self | identity_answer
    source_turn: int = 0
    evidence_text: str = ""
    identity_epoch: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "value": self.value,
            "subject": self.subject,
            "source": self.source,
            "source_turn": self.source_turn,
            "evidence_text": self.evidence_text,
            "identity_epoch": self.identity_epoch,
        }


@dataclass
class ToolRecord:
    """一次工具调用的压缩记录（全量结果太长，提示里只注入摘要）。"""

    turn: int
    name: str
    args: Dict[str, Any]
    status: str  # ok | error
    summary: str  # 结果压缩摘要
    data: Optional[Dict[str, Any]] = None  # execute_action 成功时的真实业务结果（供回核对账/生成回复）


@dataclass
class IssueResult:
    """单个问题在本轮决议中的状态（供 FinalDecision 汇总）。"""

    issue_id: str
    category: str
    status: str  # resolved | pending_user | need_info | escalated
    completion_basis: str = ""  # action_executed | user_confirmed | answer_delivered | ""
    goal: str = ""


@dataclass
class FinalDecision:
    """统一的最终决议对象 —— 回复、会话状态、交接和日志的唯一事实源。

    模型给出的 state 只是候选；最终对外状态由代码依据业务事实（完成依据、
    待补充信息、待执行步骤、人工交接）计算，保证 API 响应、会话派生标志、
    前端状态标签与 trace 完全一致。
    """

    state: str  # resolved | pending_user | need_info | escalated
    issue_results: List[IssueResult]
    receipt_ids: List[str]
    next_questions: List[str]
    next_steps: List[str]
    handoff: Optional[Dict[str, Any]]
    confidence: float
    reply: str
    evidence: List[str]


@dataclass
class SessionState:
    """单个会话的全部状态。

    Step 2：多问题状态以 issues: Dict[issue_id, IssueContext] 存储，active_issue_id
    指向当前处理的问题。issue 是一个向后兼容属性，读写均作用于活动问题。
    旧问题的症状/尝试/疑点在新问题创建后仍然保留，不会丢失。
    """

    session_id: str
    created_at: datetime = field(default_factory=datetime.now)
    messages: List[Dict[str, Any]] = field(default_factory=list)  # OpenAI 格式全量历史
    # 多问题存储：issue_id -> IssueContext；至少有一个问题
    issues: Dict[str, IssueContext] = field(default_factory=lambda: {})
    active_issue_id: str = ""
    hypotheses: List[Hypothesis] = field(default_factory=list)
    tool_records: List[ToolRecord] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.issues:
            default_issue = IssueContext()
            self.issues[default_issue.issue_id] = default_issue
            self.active_issue_id = default_issue.issue_id
        elif not self.active_issue_id or self.active_issue_id not in self.issues:
            self.active_issue_id = next(iter(self.issues))

    @property
    def issue(self) -> IssueContext:
        """当前活动问题（向后兼容：所有 state.issue.* 读写均作用于此）。"""
        return self.issues[self.active_issue_id]

    @issue.setter
    def issue(self, value: IssueContext) -> None:
        """替换活动问题：把新问题写入 issues 并切换 active_issue_id。"""
        if not isinstance(value, IssueContext):
            return
        self.issues[value.issue_id] = value
        self.active_issue_id = value.issue_id

    # ---- 多问题管理 ----

    def create_issue(self, category: str = "unknown", description: str = "") -> IssueContext:
        """创建一个新问题并设为活动问题；旧问题保留在 issues 中不丢失。

        用于 resolved 后用户提出新问题，或明确区分多个并行问题时。
        """
        new_issue = IssueContext(category=category, description=description)
        self.issues[new_issue.issue_id] = new_issue
        self.active_issue_id = new_issue.issue_id
        return new_issue

    def get_issue(self, issue_id: str) -> Optional[IssueContext]:
        return self.issues.get(issue_id)

    # lookup_user 最近一次命中的目录记录（仅作查询上下文/信息展示，不能作为可信身份）。
    # 可信身份单独存于 verified_user，避免后续目录查询把已核验主体悄悄替换掉。
    user: Optional[Dict[str, Any]] = None
    # 已通过核验的身份主体（mark_identity_verified 写入，动作目标绑定与角色判定的唯一可信来源）。
    # LLM 无法通过 issue_update 提升或篡改此字段。
    verified_user: Optional[Dict[str, Any]] = None
    identity_lookup_no_match: bool = False  # lookup_user 返回 0 条命中（查无此人）
    # 身份声明记录：保存用户消息中声明的工号/部门、目录记录的对应字段、以及核验结果，
    # 便于追溯核验依据，避免在多处叠加关键词补丁。
    identity_claim: Optional[Dict[str, Any]] = None
    # 跨轮次身份声明累积：用户可能在第 1 轮只报工号、第 2 轮才报部门。
    # 记录最近一次用户消息中声明的工号/部门，与本轮声明合并后再核验（D02）。
    # 每个字段附带来源（user_message）和声明轮次，便于追溯核验依据，
    # 满足"记录字段来源和轮次"的要求。
    last_claimed_eid: str = ""
    last_claimed_dept: str = ""
    last_claimed_eid_source: str = ""  # 字段来源：目前仅 user_message
    last_claimed_dept_source: str = ""
    last_claimed_eid_turn: int = 0    # 声明所在轮次
    last_claimed_dept_turn: int = 0
    # Step 2：结构化的本人部门声明（只有明确归属于本人的声明才会被保存）。
    # last_claimed_dept 是其 value 的快捷镜像，向后兼容。
    department_claim: Optional[DepartmentClaim] = None
    # Step 2：身份上下文编号。用户声明新的本人工号时递增，
    # 使旧 epoch 的部门声明自然失效，避免把 A 的部门转移给 B。
    identity_epoch: int = 0
    # Step 2：待回答的身份问题（由代码的身份核验流程创建，回答后消费）。
    # 结构：{"field": "department"|"employee_id", "subject": "self",
    #        "asked_turn": int, "identity_epoch": int}
    # 不能仅凭模型说"现在正在核验"就创建。
    pending_identity_question: Optional[Dict[str, Any]] = None
    clarified_gaps: List[str] = field(default_factory=list)  # 已追问过的信息缺口
    turn_count: int = 0
    consecutive_need_info: int = 0
    consecutive_tool_errors: int = 0
    identity_ask_count: int = 0
    final_retry_used: bool = False  # 每轮重置
    last_clarify_turn: int = -99  # 最近一次"改写为追问"发生的轮次（检测"无新证据"用）
    # C01：最近一次追问时的症状/疑点快照，用于检测用户是否补充了新事实。
    # 用户补充地点、版本、错误码、尝试结果等也算进展，不能只看工具调用。
    last_clarify_symptoms_count: int = 0
    last_clarify_questions_count: int = 0
    last_clarify_attempted_count: int = 0
    escalated: bool = False
    resolved: bool = False
    # 多问题状态分离：按 category 分别追踪已解决/已见问题类别。
    # 只有所有已见类别都已解决，才标记整场会话 resolved —— 避免密码问题解决后
    # 把仍未解决的 VPN 问题一起关闭（S02）。
    resolved_categories: set = field(default_factory=set)
    seen_categories: set = field(default_factory=set)
    escalation_ticket: Optional[Dict[str, Any]] = None
    total_usage: Dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})

    # ---- 身份核验（代码唯一可写入口）----

    def mark_identity_verified(self, user: Dict[str, Any]) -> None:
        """由代码在 lookup_user 双字段比对通过后调用，置位可信身份。

        - 只写入 verified_user（可信主体），不覆盖 state.user（最近一次目录查询结果）。
        - LLM 无法通过 issue_update.verified_identity 提升此状态（merge 已忽略该字段）。
        - Step 4：同步建立 department_claim（来自已核验目录记录），
          使 has_valid_identity_verification() 在核验后立即可通过。
          生产中此方法仅在部门声明匹配目录后调用，故 claim 与用户声明一致。
        """
        self.verified_user = user
        self.issue.verified_identity = True
        self.issue.user_id = user.get("employee_id", self.issue.user_id)
        # 建立与已核验主体一致的部门声明（若用户已声明则沿用，否则从目录记录补齐）
        dir_dept = (user.get("department") or "").strip().lower()
        if dir_dept:
            existing = self.department_claim
            if existing is None or existing.value != dir_dept \
                    or existing.identity_epoch != self.identity_epoch:
                self.department_claim = DepartmentClaim(
                    value=dir_dept,
                    subject="self",
                    source=existing.source if existing else "explicit_self",
                    source_turn=existing.source_turn if existing else self.turn_count,
                    evidence_text=existing.evidence_text if existing else "",
                    identity_epoch=self.identity_epoch,
                )
                self.last_claimed_dept = dir_dept
                self.last_claimed_dept_source = self.department_claim.source
                self.last_claimed_dept_turn = self.department_claim.source_turn

    def reset_identity_verification(self) -> None:
        """身份切换时清空已核验主体，要求重新核验。

        场景：已核验 E1001 后，用户又让查 E1002 的目录 —— lookup_user 命中 E1002，
        此时必须丢弃 E1001 的核验状态，不能把 E1001 的认证转移给 E1002。
        同时清空跨轮累积的身份声明，避免旧工号/部门残留干扰新主体核验。

        Step 2：同步清空 department_claim 与 pending_identity_question，
        并递增 identity_epoch，使旧身份上下文的部门声明自然失效。
        """
        self.verified_user = None
        self.issue.verified_identity = False
        self.identity_claim = None
        self.last_claimed_eid = ""
        self.last_claimed_dept = ""
        self.last_claimed_eid_source = ""
        self.last_claimed_dept_source = ""
        self.last_claimed_eid_turn = 0
        self.last_claimed_dept_turn = 0
        self.department_claim = None
        self.pending_identity_question = None
        self.identity_epoch += 1

    @property
    def verified_subject(self) -> Optional[Dict[str, Any]]:
        """已通过核验的身份主体（动作目标绑定与角色判定的唯一可信来源）。

        优先用 verified_user（mark_identity_verified 写入）；若为 None 但
        verified_identity 为 True（旧版/测试直接置位的兼容路径），回退到当前 user。
        安全保证：身份切换时 _cache_user_directory 会同步重置 verified_identity，
        因此此回退不会读到"被切换后的 user"。
        """
        if self.verified_user is not None:
            return self.verified_user
        if self.issue.verified_identity:
            return self.user
        return None

    def has_valid_identity_verification(self) -> bool:
        """Step 4：检查"已核验"是否仍然有效，而不仅看布尔值。

        有效核验必须同时满足：
        1. verified_identity 为 True 且有已核验主体；
        2. 存在本人部门声明（department_claim.subject == "self"）；
        3. 部门声明属于当前身份上下文（identity_epoch 一致）；
        4. 声明部门与已核验主体的目录部门一致。

        任何一条不满足即视为无效（例如用户本轮撤回了部门声明、或更换了工号），
        动作执行闸门应拒绝执行。
        """
        if not self.issue.verified_identity:
            return False
        subject = self.verified_subject
        if not subject:
            return False
        claim = self.department_claim
        if claim is None or claim.subject != "self":
            return False
        if claim.identity_epoch != self.identity_epoch:
            return False
        dir_dept = (subject.get("department") or "").strip().lower()
        if claim.value != dir_dept:
            return False
        return True

    # ---- 消息操作 ----

    def append_message(self, role: str, content: str) -> None:
        self.messages.append({"role": role, "content": content})

    def append_assistant_tool_calls(self, resp) -> None:
        """把带 tool_calls 的 assistant 消息原样存入历史（OpenAI 协议要求回传）。"""
        msg: Dict[str, Any] = {
            "role": "assistant",
            "content": resp.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json_dumps(tc.arguments),
                    },
                }
                for tc in resp.tool_calls
            ],
        }
        self.messages.append(msg)

    def append_tool_result(self, call_id: str, result: Dict[str, Any]) -> None:
        self.messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": json_dumps(result),
            }
        )

    def append_usage(self, usage) -> None:
        if usage is None:
            return
        self.total_usage["prompt"] += usage.prompt_tokens
        self.total_usage["completion"] += usage.completion_tokens

    # ---- 派生信息 ----

    def new_tool_evidence_since(self, turn_mark: int) -> bool:
        """自 turn_mark 之后是否出现过成功的工具结果（判定"有无新证据"）。"""
        return any(r.turn > turn_mark and r.status == "ok" for r in self.tool_records)

    def has_new_progress_since(self, turn_mark: int) -> bool:
        """自 turn_mark 之后是否有新进展（工具结果 或 用户补充的新事实）。

        C01：用户补充地点、版本、错误码、尝试结果等新事实时，即使没有新工具调用，
        也应视为有进展，避免误判为澄清循环。
        判断依据：症状/已尝试/疑点数量较上次追问快照有变化。
        """
        # 新工具结果
        if self.new_tool_evidence_since(turn_mark):
            return True
        # 用户补充的新事实：症状、已尝试方案、疑点数量变化
        if len(self.issue.symptoms) > self.last_clarify_symptoms_count:
            return True
        if len(self.issue.attempted) > self.last_clarify_attempted_count:
            return True
        # 疑点数量变化（新增或关闭都算进展）
        if len(self.issue.unresolved_questions) != self.last_clarify_questions_count:
            return True
        return False

    def snapshot_clarify_state(self) -> None:
        """在改写为追问时快照症状/疑点/已尝试数量，供后续进展判断使用（C01）。"""
        self.last_clarify_symptoms_count = len(self.issue.symptoms)
        self.last_clarify_questions_count = len(self.issue.unresolved_questions)
        self.last_clarify_attempted_count = len(self.issue.attempted)

    def apply_final(self, final: Dict[str, Any]) -> None:
        """把通过审查的最终结论落到状态卡（合并 + 状态标志）。"""
        self.issue.merge(final.get("issue_update", {}))
        self._merge_hypotheses(final.get("issue_update", {}) or {})
        self._apply_state_flags(final)

    def apply_final_state_flags(
        self, final: Dict[str, Any], computed_state: Optional[str] = None
    ) -> None:
        """只设状态标志（issue 与 hypotheses 已在对账前合并，不重复 merge）。

        computed_state 为代码依据 FinalDecision 算出的对外状态；若提供则优先使用，
        保证会话派生标志与 API 响应、trace 完全一致，避免两套结论。
        """
        self._apply_state_flags(final, computed_state=computed_state)

    def _apply_state_flags(
        self, final: Dict[str, Any], computed_state: Optional[str] = None
    ) -> None:
        state = computed_state if computed_state is not None else final.get("state")
        # 同步活动问题的状态字段（与对外决议一致）
        if state in ("resolved", "pending_user", "need_info", "escalated"):
            self.issue.status = state
        # 多问题状态分离：按 category 追踪，而非整场会话一刀切。
        # 密码 resolved 不影响 VPN 的未决状态（S02）。
        cat = self.issue.category or "unknown"
        self.seen_categories.add(cat)
        if state == "escalated":
            self.escalated = True
        elif state == "resolved":
            # S02：仍有未解决疑点时，不能把该类别标记为已解决。
            # 密码重置成功但 VPN 仍有未解决疑点时，VPN 类别不能被 resolved 覆盖。
            has_open_questions = any(
                q.status == "open" for q in self.issue.unresolved_questions
            )
            if not has_open_questions:
                self.resolved_categories.add(cat)
            # 只有所有已见类别都已解决，才标记整场会话 resolved
            self.resolved = bool(self.seen_categories) and self.seen_categories.issubset(
                self.resolved_categories
            )

    def _merge_hypotheses(self, issue_update: Dict[str, Any]) -> None:
        for h in issue_update.get("hypotheses", []) or []:
            if isinstance(h, dict) and h.get("text"):
                self._upsert_hypothesis(h)

    def _upsert_hypothesis(self, h: Dict[str, Any]) -> None:
        # D03：evidence 可能不是列表（LLM 误填为 int/str 等），必须先归一化，
        # 否则 for ev in evidence 会抛 TypeError 导致整轮崩溃。
        ev_raw = h.get("evidence", [])
        if isinstance(ev_raw, list):
            ev_list = [e for e in ev_raw if isinstance(e, str)]
        else:
            ev_list = []
        status = h.get("status", "testing")
        # M04/M05：rejected 必须有针对该假设的反证，不能仅凭 healthy/其他正常等
        # 弱证据排除。若所有证据都是弱信号（healthy、正常、无故障等），降级为 testing。
        if status == "rejected" and ev_list:
            weak_patterns = ["healthy", "正常", "无故障", "no issue", "running", "available"]
            all_weak = all(
                any(p in e.lower() for p in weak_patterns) for e in ev_list
            )
            if all_weak:
                status = "testing"
        for existing in self.hypotheses:
            if existing.text == h["text"]:
                existing.status = status
                for ev in ev_list:
                    if ev not in existing.evidence:
                        existing.evidence.append(ev)
                return
        self.hypotheses.append(
            Hypothesis(
                text=h["text"],
                status=status,
                evidence=ev_list,
            )
        )

    # ---- 序列化（供 API 与评测报告使用）----

    def to_dict(self) -> Dict[str, Any]:
        def _issue_to_dict(iss: IssueContext) -> Dict[str, Any]:
            d = iss.__dict__.copy()
            d["conflicts"] = [c.__dict__.copy() for c in iss.conflicts]
            d["unresolved_questions"] = [q.__dict__.copy() for q in iss.unresolved_questions]
            return d

        issue_dict = _issue_to_dict(self.issue)
        issues_dict = {iid: _issue_to_dict(iss) for iid, iss in self.issues.items()}
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "turn_count": self.turn_count,
            "issue": issue_dict,
            "issues": issues_dict,
            "active_issue_id": self.active_issue_id,
            "hypotheses": [h.__dict__.copy() for h in self.hypotheses],
            "tool_records": [r.__dict__.copy() for r in self.tool_records],
            "user": self.user,
            "verified_user": self.verified_user,
            "identity_claim": self.identity_claim,
            "last_claimed_eid": self.last_claimed_eid,
            "last_claimed_dept": self.last_claimed_dept,
            "last_claimed_eid_source": self.last_claimed_eid_source,
            "last_claimed_dept_source": self.last_claimed_dept_source,
            "last_claimed_eid_turn": self.last_claimed_eid_turn,
            "last_claimed_dept_turn": self.last_claimed_dept_turn,
            "department_claim": self.department_claim.to_dict() if self.department_claim else None,
            "identity_epoch": self.identity_epoch,
            "pending_identity_question": self.pending_identity_question,
            "escalated": self.escalated,
            "resolved": self.resolved,
            "resolved_categories": sorted(self.resolved_categories),
            "seen_categories": sorted(self.seen_categories),
            "escalation_ticket": self.escalation_ticket,
            "total_usage": self.total_usage,
            "message_count": len(self.messages),
        }


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]
