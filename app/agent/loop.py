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
    IDENTITY_ACTIONS, READONLY_TOOLS, OutcomeEngine, force_escalation_final, normalize_final,
)
from app.agent.prompts import build_messages, build_system_prompt, summarize_tool_result
from app.agent.state import (
    FinalDecision, IssueContext, IssueResult, SessionState, ToolRecord,
    DepartmentClaim, new_session_id,
)
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


def _strip_quoted_email_context(text: str) -> str:
    """剔除被引用的邮件/原文语境，返回可用于本人声明匹配的文本。

    Step 1：邮件正文、转发、原文等语境中出现的部门是被引用内容，不是本人声明。
    例如"邮件里写着'我的部门是 engineering'"中的 engineering 必须被剔除。
    识别方式：若文本包含引用标记（邮件里写着/邮件正文/原文是/转发/引用等），
    则把引号内的内容以及引用标记所在子句移除。
    """
    import re as _re
    # 只保留明确表示"以下是外部引用/转发内容"的标记。
    # "界面/按钮/标签/写着/显示为"等是 UI 描述词，会误伤"我的部门是'engineering'。登录界面报错"
    # 这类合法本人声明，故移除。
    quote_markers = [
        "邮件里写着", "邮件中写着", "邮件正文", "邮件里说", "邮件中说",
        "原文是", "原文如下", "转发", "引用", "如下内容",
    ]
    has_quote_ctx = any(m in text for m in quote_markers)
    if not has_quote_ctx:
        return text
    # 移除引号包裹的内容（中英文引号）
    quote_pairs = [
        ("\u201c", "\u201d"), ("\u300c", "\u300d"),
        ("\u2018", "\u2019"), ('"', '"'), ("'", "'"),
    ]
    result = text
    for open_q, close_q in quote_pairs:
        if open_q == close_q:
            # 同字符引号：非贪婪匹配
            pattern = _re.escape(open_q) + r"[^" + _re.escape(open_q) + r"]*" + _re.escape(close_q)
        else:
            pattern = _re.escape(open_q) + r"[^" + _re.escape(close_q) + r"]*" + _re.escape(close_q)
        result = _re.sub(pattern, "", result)
    # 移除包含引用标记的子句
    clauses = _re.split(r"[，,;；。.!！?？、]", result)
    kept = []
    for c in clauses:
        if any(m in c for m in quote_markers):
            continue
        kept.append(c)
    return " ".join(kept)


def _match_alias_word_boundary(text: str, alias: str) -> bool:
    """用词边界匹配部门别名，避免 it/hr 等短词被普通英文子串误命中。

    - 英文别名：\b{alias}\b（忽略大小写）
    - 中文别名：前后非汉字边界
    """
    import re as _re
    alias_lower = alias.lower()
    if _re.search(r"[A-Za-z]", alias):
        return _re.search(r"\b" + _re.escape(alias_lower) + r"\b", text, _re.IGNORECASE) is not None
    # 中文别名：前后不能是汉字
    pattern = r"(?<![\u4e00-\u9fa5])" + _re.escape(alias) + r"(?![\u4e00-\u9fa5])"
    return _re.search(pattern, text) is not None


def _canonicalize_department_value(raw_value: str) -> str:
    """把提取到的原始部门值（可能带引号/空白/中文）规范化为目录标准部门名。

    返回空串表示无法识别为已知部门。
    """
    import re as _re
    # 剥掉引号与空白（使用显式 Unicode 转义，与 _DEPT_QUOTE_CLASS 保持一致）
    quote_chars = "\"'\u201c\u201d\u2018\u2019\u300c\u300d"
    v = raw_value.strip().strip(quote_chars)
    v = v.strip()
    if not v:
        return ""
    # 剥离中文部门后缀"部门"/"部"，使"财务部"→"财务"能匹配别名
    v = _re.sub(r"(部门|部)$", "", v).strip()
    if not v:
        return ""
    v_lower = v.lower()
    # 按别名长度降序匹配，长别名优先
    all_aliases = []
    for canonical, aliases in _DEPARTMENT_ALIASES.items():
        for alias in aliases:
            all_aliases.append((canonical, alias))
    all_aliases.sort(key=lambda x: len(x[1]), reverse=True)
    for canonical, alias in all_aliases:
        if _match_alias_word_boundary(v, alias):
            return canonical
    return ""


# 明确本人部门声明的句式模板（按优先级排列）。
# {dept} 占位符将被替换为部门值捕获组。
_SELF_DEPT_PATTERNS = [
    # 我的(所在)部门是/为/：X
    r"我的(?:所在)?部门(?:是|为|[:：])\s*{dept}",
    # 本人(所在)部门是/为/：X
    r"本人(?:所在)?部门(?:是|为|[:：])\s*{dept}",
    # 我在/属于 X 部门
    r"我(?:在|属于)\s*{dept}\s*部门",
    # 我属于 X
    r"我属于\s*{dept}",
    # 我所在的部门是 X
    r"我所在的部门(?:是|为|[:：])\s*{dept}",
    # my department is/: X
    r"my\s+department\s+(?:is|:)\s*{dept}",
    # i am / i'm in X department
    r"i(?:\s+am|'m)\s+in\s+{dept}\s+department",
    # i belong to X
    r"i\s+belong\s+to\s+{dept}",
]

# 部门值捕获：英文（含下划线）或中文（可选"部"后缀），可带引号包裹。
# 使用显式 Unicode 转义表示各类引号，避免源码编码导致字符丢失：
# \u201c " \u201d "  \u2018 ' \u2019 '  \u300c 「 \u300d 」
_DEPT_QUOTE_CLASS = r"['\"\u201c\u201d\u2018\u2019\u300c\u300d]"
_DEPT_VALUE_CAPTURE = _DEPT_QUOTE_CLASS + r"?\s*([A-Za-z_]{2,}|[\u4e00-\u9fa5]{2,}(?:部)?)\s*" + _DEPT_QUOTE_CLASS + r"?"

# 仅当同一消息中存在本人工号声明时才启用的弱本人部门声明模式。
# 与本人工号同现的"X部门/X部"几乎必然是本人部门（如"工号E1001，engineering部门"）。
# 注意：不含"同事/team/团队"——"engineering 的同事"是他人，不是本人。
# 主语切换与否定由 _match_explicit_self_department 中的守卫过滤。
_SELF_DEPT_WITH_EID_PATTERNS = [
    r"{dept}\s*部门",
    r"{dept}\s*部(?!门)",
    # 与本人工号同现的"X 的承包商/员工/职员"结构（如"engineering 的承包商"），
    # 部门名修饰身份词，表明用户属于该部门。
    r"{dept}\s*的?\s*(?:承包商|员工|职员|人员)",
]

# 弱本人声明的"主语切换/否定"标记：命中这些词的子句中，"X部门"不视为本人声明。
# 用于防止"我的同事在 engineering 部门"、"不是 finance 部门"被误判为本人声明。
_WEAK_DEPT_GUARD_MARKERS = [
    "同事", "朋友", "他的", "她的", "别人", "他们的",
    "不是", "并非", "非", "不属", "不属于",
    "邮件", "转发", "原文", "引用", "回复",
]


def _match_explicit_self_department(
    text: str, self_eid_present: bool = False
) -> Optional[DepartmentClaim]:
    """在文本中匹配明确的本人部门声明，返回 DepartmentClaim 或 None。

    Step 1：只接受正向句式匹配，不再做全文扫描兜底。
    多个不同声明 → 歧义，返回 None。

    当 self_eid_present=True（同一消息中用户声明了本人工号）时，
    额外接受"X部门/X部"结构（如"engineering部门"、"财务部"），
    因为与本人工号同现的部门几乎必然是本人部门。
    """
    import re as _re
    if not text:
        return None
    found = []
    for pat_tpl in _SELF_DEPT_PATTERNS:
        pattern = pat_tpl.format(dept=_DEPT_VALUE_CAPTURE)
        for m in _re.finditer(pattern, text, _re.IGNORECASE):
            raw = m.group(1)
            canonical = _canonicalize_department_value(raw)
            if canonical:
                found.append((canonical, m.group(0).strip()))
    # 与本人工号同现的"X部门/X部"结构（弱本人声明，需 self_eid_present）
    if self_eid_present:
        # 按标点拆子句，用于判断弱候选所在子句是否含主语切换/否定标记
        clauses = _re.split(r"[，,;；。.!！?？、\n]", text)
        for pat_tpl in _SELF_DEPT_WITH_EID_PATTERNS:
            pattern = pat_tpl.format(dept=_DEPT_VALUE_CAPTURE)
            for m in _re.finditer(pattern, text, _re.IGNORECASE):
                raw = m.group(1)
                canonical = _canonicalize_department_value(raw)
                if not canonical:
                    continue
                # 守卫：找到匹配所在子句，若含主语切换/否定标记则丢弃该候选。
                # 例如"我的同事在 engineering 部门"中 engineering 被丢弃；
                # "我的部门是 engineering，不是 finance 部门"中 finance 被丢弃。
                match_start = m.start()
                clause_of_match = ""
                pos = 0
                for c in clauses:
                    c_start = text.find(c, pos)
                    if c_start == -1:
                        break
                    c_end = c_start + len(c)
                    if c_start <= match_start < c_end:
                        clause_of_match = c
                        break
                    pos = c_end
                if any(g in clause_of_match for g in _WEAK_DEPT_GUARD_MARKERS):
                    continue
                found.append((canonical, m.group(0).strip()))
    if not found:
        return None
    # 去重（同一部门值的多次匹配视为同一声明）
    unique = {}
    for canonical, evidence in found:
        unique.setdefault(canonical, evidence)
    if len(unique) > 1:
        return None  # 多候选歧义
    canonical, evidence = next(iter(unique.items()))
    return DepartmentClaim(
        value=canonical,
        subject="self",
        source="explicit_self",
        evidence_text=evidence,
    )


def _match_department_as_answer(text: str) -> Optional[DepartmentClaim]:
    """身份问答模式：在 pending_identity_question 存在时接受用户回答的部门。

    Step 1（二阶段收紧）：不再独立全文扫描，而是复用本人声明的归属判断：
    1) 先尝试明确本人声明句式（_match_explicit_self_department），它已内置
       主语切换/否定/引用守卫；命中则直接采用。
    2) 否则做宽松扫描（用户直接回答部门值，如"engineering"），但必须：
       - 先剥离引用语境（邮件/转发/原文中的部门不算本人回答）；
       - 按子句过滤：含否定或第三方标记的子句中的部门不接受。
    仅当恰好一个部门通过所有过滤时才接受。
    """
    import re as _re
    if not text:
        return None

    # 先剥离引用语境（邮件/转发/原文），再做本人声明匹配，
    # 避免引号内的"我的部门是 engineering"被当成本人回答。
    text_for_self = _strip_quoted_email_context(text)
    if not text_for_self:
        return None

    # 1) 先复用明确本人声明匹配（含主语/否定守卫）
    explicit = _match_explicit_self_department(text_for_self, self_eid_present=False)
    if explicit is not None:
        explicit.source = "identity_answer"
        return explicit

    # 2) 宽松扫描：在已剥离引用的文本上进行
    text_for_scan = text_for_self

    all_aliases = []
    for canonical, aliases in _DEPARTMENT_ALIASES.items():
        for alias in aliases:
            all_aliases.append((canonical, alias))
    all_aliases.sort(key=lambda x: len(x[1]), reverse=True)

    # 按子句拆分，用于否定/第三方守卫
    clauses = _re.split(r"[，,;；。.!！?？、\n]", text_for_scan)

    found = set()
    evidence = ""
    for canonical, alias in all_aliases:
        if not _match_alias_word_boundary(text_for_scan, alias):
            continue
        # 找到该别名所在子句，检查是否含否定或第三方标记
        alias_idx = text_for_scan.lower().find(alias.lower())
        clause_of_alias = ""
        pos = 0
        for c in clauses:
            c_start = text_for_scan.find(c, pos)
            if c_start == -1:
                break
            c_end = c_start + len(c)
            if c_start <= alias_idx < c_end:
                clause_of_alias = c
                break
            pos = c_end
        if any(g in clause_of_alias for g in _WEAK_DEPT_GUARD_MARKERS):
            continue
        found.add(canonical)
        if not evidence:
            evidence = alias

    if len(found) != 1:
        return None  # 零个或多个 → 不接受
    canonical = found.pop()
    return DepartmentClaim(
        value=canonical,
        subject="self",
        source="identity_answer",
        evidence_text=evidence,
    )


def _extract_department(
    text: str, pending_question: Optional[Dict[str, Any]] = None,
    self_eid_present: bool = False,
) -> Optional[DepartmentClaim]:
    """从文本中提取本人部门声明，返回 DepartmentClaim；找不到有效声明返回 None。

    Step 1 核心原则：部门只能来自两类来源——
    1. 明确的本人声明：我的部门是 X、本人属于 X、我在 X 部门。
       （同一消息含本人工号时，"X部门/X部"也视为本人声明。）
    2. 回答系统的身份问题：代码记录了 pending_identity_question（field=department），
       用户直接回答部门值。

    其他任何出现的部门名（邮件正文、引用、举例、同事信息、裸词）一律不接受。
    不再做"扫描全文找部门"的兜底。

    引号处理：
    - 我的部门是"engineering"：引号是值的定界符，剥掉后按本人声明处理。
    - 邮件里写着"我的部门是 engineering"：整句属于引用语境，剔除后不接受。
    """
    if not text:
        return None
    import re as _re

    # 第零步：用户明确表示不提供本人部门 → 任何部门都不算本人声明。
    no_dept_markers = [
        "我不提供本人部门", "我不提供自己的部门", "不提供我的部门",
        "我不提供部门", "不提供本人部门", "不提供自己的部门",
        "i don't provide", "i'm not providing", "not providing my",
    ]
    if any(m in text.lower() for m in no_dept_markers):
        return None

    # 第一步：剔除被引用的邮件/原文语境，保留可用于本人声明匹配的文本。
    text_for_self = _strip_quoted_email_context(text)

    # 第二步：明确本人声明模式（正向句式匹配，优先级最高）
    claim = _match_explicit_self_department(text_for_self, self_eid_present=self_eid_present)
    if claim is not None:
        return claim

    # 第三步：身份问答模式（仅当代码确实追问过本人部门时）
    if pending_question and pending_question.get("field") == "department" \
            and pending_question.get("subject") == "self":
        claim = _match_department_as_answer(text)
        if claim is not None:
            return claim

    # 未命中任何有效来源 → 返回 None（不再扫描全文）
    return None


def _extract_employee_id(text: str) -> str:
    """从文本中提取工号（E 开头加数字），找不到返回空串。"""
    if not text:
        return ""
    import re as _re
    m = _re.search(r"[Ee]\d{3,}", text)
    return m.group(0).upper() if m else ""


# 本人身份声明的上下文标记词：出现在工号之前，表明该工号是说话人本人
_SELF_DECLARATION_PREFIXES = [
    "我是", "我叫", "我工号", "我的工号", "本人", "我本人", "这是我",
    "工号是", "工号为", "我的id", "我的编号", "员工编号",
]
# 操作对象的上下文标记词：出现在工号之前，表明该工号是被操作的他人
_OPERATION_TARGET_PREFIXES = [
    "帮", "给", "为", "替", "同事", "朋友", "他的", "她的", "它的",
    "重置", "解锁", "授权", "开通", "申请", "查询",
]


def _extract_self_employee_id(text: str) -> str:
    """提取本人身份声明的工号（Step 3：区分本人与操作对象）。

    仅当工号出现在本人声明语境中时才返回；"帮同事 E1002 重置密码"中的 E1002
    是操作对象，不能作为本人身份。
    """
    if not text:
        return ""
    import re as _re
    for prefix in _SELF_DECLARATION_PREFIXES:
        m = _re.search(_re.escape(prefix) + r"\s*[Ee](\d{3,})", text)
        if m:
            return "E" + m.group(1)
    # 孤立工号（无前缀）：若同时存在操作对象前缀，则不视为本人
    standalone = _re.search(r"(?<![A-Za-z])[Ee](\d{3,})", text)
    if standalone:
        eid = "E" + standalone.group(1)
        for prefix in _OPERATION_TARGET_PREFIXES:
            if prefix in text[:standalone.start()]:
                return ""  # 该工号是操作对象，不是本人
        return eid
    return ""


def _extract_operation_target(text: str) -> str:
    """提取操作对象工号（用于动作目标绑定，不作为本人身份）。"""
    if not text:
        return ""
    import re as _re
    for prefix in _OPERATION_TARGET_PREFIXES:
        m = _re.search(_re.escape(prefix) + r"\s*[Ee](\d{3,})", text)
        if m:
            return "E" + m.group(1)
    return ""


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
            old_issue.status = "resolved"  # 标记旧问题已解决（保留在 issues 中供追溯）
            state.resolved = False
            # Step 2：用 create_issue 创建新问题，旧问题保留在 issues 中不丢失
            state.create_issue()
            # S02：身份核验是会话级而非问题卡级 —— 同一用户的不同问题共用已核验身份，
            # 不能因为新建问题卡就把 verified_identity 置回 False，否则上一问题已成功
            # 执行的敏感动作（如 reset_password）会在本轮被 outcome 规则 1 重新判定为
            # policy_forbidden，导致 VPN 等后续问题被错误升级。
            state.issue.verified_identity = old_issue.verified_identity
            state.issue.user_id = old_issue.user_id
            # S02：继承上一问题卡中未解决的症状和疑点，避免 VPN 等并行问题状态丢失
            state.issue.symptoms = list(old_issue.symptoms)
            state.issue.unresolved_questions = [
                q for q in old_issue.unresolved_questions if q.status == "open"
            ]
            state.issue.conflicts = [c for c in old_issue.conflicts if c.status == "active"]
            state.hypotheses = []
            state.consecutive_need_info = 0
            # 新问题重置类别追踪；未解决症状会随新问题卡重新归类
            state.resolved_categories = set()
            state.seen_categories = set()
        msg_appended = False
        identity_processed = False
        if state.escalated:
            # 升级后仍允许补充信息：记录到工单并回复确认，不进入诊断循环。
            # 但身份更正/撤回仍需处理：用户可能在升级后纠正部门，
            # 若重新核验通过则解除升级、继续正常处理（Step 3）。
            state.append_message("user", user_message)
            msg_appended = True
            self._process_identity_corrections(state)
            self._save_identity_claims(state)
            identity_processed = True
            # 尝试重新核验：用户补充了正确身份信息 → 解除升级继续处理
            self._check_identity_claim(state)
            if state.has_valid_identity_verification():
                state.escalated = False
                state.escalation_ticket = None
                # 落入下方正常流程继续执行（注意：身份声明已在上方处理，
                # 下方不再重复 _process_identity_corrections，避免把刚核验通过的
                # 状态因"更正"关键词再次 reset）。
            else:
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

        if not msg_appended:
            state.append_message("user", user_message)
        state.turn_count += 1
        state.final_retry_used = False
        state.consecutive_tool_errors = 0

        # Step 3：在 LLM 循环之前处理本轮身份更正/撤回。
        # 必须在任何工具执行前完成，否则已撤回的部门声明仍会让动作通过闸门。
        # 若上方 escalated 分支已处理过（含解除升级的情况），此处跳过，避免重复
        # 处理导致刚核验通过的状态被"更正"关键词误重置。
        if not identity_processed:
            self._process_identity_corrections(state)
            # 保存本轮新的本人声明（工号/部门），使循环内的动作能看到最新声明。
            self._save_identity_claims(state)

        # Step 5：明确的本人转人工请求 → 确定性交接流程，不被诊断工具调用阻塞。
        # 否定语境（"我不想转人工"）不触发。
        handoff_result = self._try_explicit_handoff(state, user_message, t0)
        if handoff_result is not None:
            return handoff_result

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

        # 先合并本轮描述性事实与假设，再做身份核验、动作对账和升级摘要。
        # 摘要必须基于本轮最新事实/假设生成，不能用上一轮的过时根因（Commit 4）。
        state.issue.merge(final.get("issue_update", {}))
        state._merge_hypotheses(final.get("issue_update", {}) or {})
        # F04：用户直接观察消解已知冲突时自动标记 resolved，
        # 避免 LLM 忘记在 issue_update.resolved_conflicts 中声明而导致冲突永久 active。
        self._auto_resolve_conflicts(state, self._latest_user_message(state))
        # S03：用户已回答的疑点自动关闭，避免 LLM 忘记移入 answered_questions 而重复追问。
        self._auto_close_answered_questions(state, self._latest_user_message(state))
        self._verify_identity(state)
        final = self._retry_identity_denied_actions(state, final)  # 重试因身份被拒的动作
        final = self._reconcile_actions(final, state)  # 声明的动作必须真实执行
        verdict = self.outcome.review(final, state)
        final = verdict.final

        # E05：回复与升级摘要中不得自行补全年份。若用户提供的日期缺少年份，
        # 而 LLM 在回复/摘要中补了年份，将其还原为用户原样的月日表达。
        self._sanitize_fabricated_years(state, final)

        # Step 1：统一最终决议入口 —— 模型 state 仅为候选，对外状态由代码依据
        # 完成依据、未解决疑点、待执行步骤计算。回复、会话标志、交接、日志均
        # 从此 FinalDecision 读取，保证各渠道表达一致。
        decision = self._compute_final_decision(final, state)
        # Step 2：把完成依据写回活动问题卡（仅代码可写，LLM merge 不覆盖）。
        # 按 issue_id 匹配活动问题，避免取到非活动问题的结果。
        active_ir = next(
            (ir for ir in decision.issue_results if ir.issue_id == state.issue.issue_id),
            None,
        )
        if active_ir is not None:
            state.issue.completion_basis = active_ir.completion_basis

        # 状态联动：追问计数 / 澄清标记（以决策状态为准）
        if decision.state == "need_info":
            state.consecutive_need_info += 1
            state.last_clarify_turn = state.turn_count
            # C01：快照当前症状/疑点/已尝试数量，供下一轮进展判断使用
            state.snapshot_clarify_state()
        else:
            state.consecutive_need_info = 0

        state.append_message("assistant", decision.reply)
        state.apply_final_state_flags(final, computed_state=decision.state)
        if decision.state == "escalated":
            esc = final.get("escalation", {}) or {}
            state.escalation_ticket = {
                "id": f"ESC-{state.session_id[:4].upper()}-{state.turn_count:03d}",
                "reason": esc.get("reason", "unspecified"),
                "urgency": esc.get("urgency", "medium"),
                "summary": esc.get("summary", ""),
            }

        latency_ms = int((time.monotonic() - t0) * 1000)
        result = self._build_turn_result(state, decision, turn_tools, latency_ms)
        if self.tracer:
            self.tracer.log_turn(state, final, verdict, result, decision=decision)
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

    # 明确转人工的关键词（不含否定语境）
    _HANDOFF_KEYWORDS = ["请转人工", "转人工", "找人工", "人工处理", "人工客服", "需要人工"]
    _HANDOFF_NEGATION = ["不想转人工", "不需要转人工", "不用转人工", "别转人工", "不转人工"]

    def _try_explicit_handoff(
        self, state: SessionState, user_message: str, t0: float
    ) -> Optional[TurnResult]:
        """Step 5：明确的本人转人工请求 → 确定性交接流程。

        - 仅匹配明确请求（"请转人工"等），否定语境（"我不想转人工"）不触发。
        - 不进入诊断循环，直接从当前状态卡合成摘要并创建/更新工单。
        - 必须写入 trace（消除提前 return 漏记日志的路径）。
        """
        if not user_message:
            return None
        # 否定语境优先排除
        if any(neg in user_message for neg in self._HANDOFF_NEGATION):
            return None
        if not any(kw in user_message for kw in self._HANDOFF_KEYWORDS):
            return None
        # Step 5：仅当"转人工"是主要请求时才触发确定性交接。
        # 若用户同时提出了可自动处理的动作请求（重置/解锁/授权等），
        # 则"转人工"只是该请求的一部分（如"MFA重置转人工"），应走正常流程，
        # 由对账与 outcome 决定部分升级，不能跳过动作执行。
        action_keywords = ["重置", "解锁", "授权", "开通", "授予", "grant", "reset", "unlock"]
        if any(kw in user_message for kw in action_keywords):
            return None

        # 确定交接的问题：默认当前未解决问题
        issue = state.issue
        # 构造 final 供 complete_summary 使用
        esc_final: Dict[str, Any] = {
            "state": "escalated",
            "confidence": 1.0,
            "user_message": user_message,
            "issue_update": {"category": issue.category, "description": issue.description},
            "actions_taken": [],
            "evidence": [],
            "escalation": {
                "reason": "user_requested",
                "urgency": "medium",
                "summary": "",
            },
        }
        esc_final = self.outcome.complete_summary(esc_final, state, force_reason="user_requested")

        # 创建或更新交接工单
        if state.escalation_ticket:
            state.escalation_ticket.setdefault("supplementary", []).append(user_message)
            ticket_id = state.escalation_ticket["id"]
        else:
            ticket_id = f"ESC-{state.session_id[:4].upper()}-{state.turn_count:03d}"
            state.escalation_ticket = {
                "id": ticket_id,
                "reason": "user_requested",
                "urgency": "medium",
                "summary": (esc_final.get("escalation") or {}).get("summary", ""),
            }
        state.escalated = True
        issue.status = "escalated"

        summary = (esc_final.get("escalation") or {}).get("summary", "")
        reply = (
            f"已为你创建人工交接工单（{ticket_id}）。"
            f"IT 同事会根据以下信息跟进：\n{summary}"
            if summary
            else f"已为你创建人工交接工单（{ticket_id}），IT 同事会尽快联系你。"
        )
        latency_ms = int((time.monotonic() - t0) * 1000)
        result = TurnResult(
            session_id=state.session_id, reply=reply, state="escalated",
            confidence=1.0, latency_ms=latency_ms,
            usage=state.total_usage.copy(),
            escalation={"reason": "user_requested", "ticket_id": ticket_id, "summary": summary},
        )
        # 消除提前 return 漏记 trace：交接也必须经过统一日志
        if self.tracer:
            from app.agent.outcome import Verdict
            self.tracer.log_turn(
                state, esc_final,
                Verdict("pass", esc_final, reason="user_requested_handoff"),
                result,
            )
        return result

    def _save_identity_claims(self, state: SessionState) -> None:
        """从最近一条用户消息中提取并保存本人身份声明（工号/部门）。

        Step 2/3：区分"本人身份"与"操作对象"。
        - 本人工号仅来自自我声明语境（"我是 E1001"、"我工号 E1001"）；
          "帮同事 E1002 重置密码"中的 E1002 是操作对象，不改变本人身份。
        - 部门只接受明确本人声明或回答系统身份问题（Step 1），保存为
          DepartmentClaim（含 subject/source/evidence/identity_epoch）。
        - 本人工号更正时，旧核验失效，identity_epoch 递增，旧部门声明自然过期。

        关键：无论 state.user（目录记录）是否已就绪，都必须先保存声明。
        这保证了"先声明部门、后查目录"的顺序也能跨轮累积（X02）。
        """
        user_msg = self._latest_user_message(state)
        cur_eid = _extract_self_employee_id(user_msg)
        # Step 1：_extract_department 返回 Optional[DepartmentClaim]，传入 pending 问题。
        # self_eid_present=True 时，"X部门"结构也视为本人声明（与本人工号同现）。
        dept_claim = _extract_department(
            user_msg, state.pending_identity_question, self_eid_present=bool(cur_eid),
        )
        # 操作对象（仅记录，不参与本人身份核验）
        op_target = _extract_operation_target(user_msg)
        if op_target:
            state.issue.user_id = op_target  # 操作目标绑定到问题卡（供动作校验）
        turn = state.turn_count

        if cur_eid:
            # 用户更正工号：新声明的工号与已核验主体不一致 → 旧核验失效，
            # 不能自动沿用原主体的部门（避免把 E1001 的认证转移给 E1002）。
            verified_eid = (state.verified_subject or {}).get("employee_id", "")
            if verified_eid and cur_eid != verified_eid:
                state.reset_identity_verification()  # 递增 identity_epoch，清空旧声明
            state.last_claimed_eid = cur_eid
            state.last_claimed_eid_source = "user_message"
            state.last_claimed_eid_turn = turn

        if dept_claim is not None:
            # 补全声明的身份上下文与轮次信息
            dept_claim.source_turn = turn
            dept_claim.identity_epoch = state.identity_epoch
            state.department_claim = dept_claim
            state.last_claimed_dept = dept_claim.value
            state.last_claimed_dept_source = dept_claim.source
            state.last_claimed_dept_turn = turn
            # 消费 pending 身份问题（回答后即清除）
            if state.pending_identity_question and state.pending_identity_question.get("field") == "department":
                state.pending_identity_question = None

        # 注意：pending_identity_question 不在此处设置。
        # 它只能由 outcome 规则 2 在系统实际追问用户时设置（供下一轮回答消费），
        # 避免同一轮内"先设 pending 又把本轮文本当回答"的自举问题。

    # 撤回/更正部门声明的关键词（中英文）。命中时撤销当前部门声明及依赖它的核验。
    # 分为强/弱两组：
    # - 强标记：明确指向部门/身份（如"部门说错"、"同事的部门"），命中即触发。
    # - 弱标记："更正/我说错/撤回"等可能指其他内容（如"更正，VPN 从昨天开始断"），
    #   仅当消息中同时出现部门/身份相关词时才视为身份更正。
    _DEPT_RETRACTION_STRONG = [
        "刚才的部门", "之前的部门", "部门说错", "部门不对", "部门错了",
        "同事的部门", "不是我的部门", "那是同事", "那是别人",
        "wrong department", "not my department", "colleague's department",
    ]
    _DEPT_RETRACTION_WEAK = [
        "我说错", "我改错", "更正", "撤回", "撤销", "收回",
        "i made a mistake", "correct that", "that was wrong", "retract",
    ]
    # 弱标记触发所需的部门/身份上下文词
    _DEPT_CONTEXT_WORDS = ["部门", "工号", "身份", "employee", "department"]

    def _process_identity_corrections(self, state: SessionState) -> None:
        """处理本轮用户对身份声明的更正/撤回（Step 3）。

        必须在 _save_identity_claims 之前、且在 early-return 之前执行，
        防止"之前误核验一次，之后一直沿用"。

        场景：
        - "刚才 engineering 是同事的部门，我说错了" → 撤销部门声明，旧核验失效
        - "更正，我属于 finance" → 含部门词，撤销旧部门声明（新声明由 _save_identity_claims 写入）
        - "更正，VPN 从昨天开始断" → 无部门/身份上下文，不触发身份撤回
        - 更换本人工号 → 已在 _save_identity_claims 中处理（identity_epoch 递增）
        - "帮同事 E1002 重置密码" → 不改本人身份（操作对象，不触发此处）
        """
        user_msg = self._latest_user_message(state)
        if not user_msg:
            return
        msg_lower = user_msg.lower()
        has_strong = any(m in user_msg or m in msg_lower for m in self._DEPT_RETRACTION_STRONG)
        has_weak = any(m in user_msg or m in msg_lower for m in self._DEPT_RETRACTION_WEAK)
        if has_weak and not has_strong:
            # 弱标记需部门/身份上下文才触发
            has_dept_context = any(
                alias in msg_lower
                for aliases in _DEPARTMENT_ALIASES.values() for alias in aliases
            ) or any(w in user_msg or w in msg_lower for w in self._DEPT_CONTEXT_WORDS)
            if not has_dept_context:
                has_weak = False
        if not (has_strong or has_weak):
            return
        # 撤回部门声明：清空 department_claim 及镜像字段，依赖它的核验必须失效
        was_verified = state.issue.verified_identity
        state.department_claim = None
        state.last_claimed_dept = ""
        state.last_claimed_dept_source = ""
        state.last_claimed_dept_turn = 0
        if was_verified:
            # 旧核验立即失效：清可信主体、置未核验、递增 epoch 使旧部门声明过期。
            # 注意：不调用 reset_identity_verification() —— 那里会清空 last_claimed_eid，
            # 但部门更正/撤回不代表工号变更，用户的本人工号声明仍然有效，应保留供重新比对。
            state.verified_user = None
            state.issue.verified_identity = False
            state.identity_claim = None
            state.identity_epoch += 1
        # 若用户在同一轮给出了新部门，_save_identity_claims 会重新写入并比对

    def _check_identity_claim(self, state: SessionState) -> None:
        """统一身份核验入口：_try_early_verify（工具执行中）与 _verify_identity（循环后）共用。

        核验流程（Step 3）：
        1. 先处理本轮身份更正/撤回（可能使已核验失效，必须在 early-return 之前）。
        2. 保存用户明确提供的本人信息（工号/部门），记录来源和轮次。
           即使尚无目录记录也必须保存，保证跨轮累积（X02）。
        3. 若已核验且声明仍有效，才沿用；否则继续比对。
        4. 核验条件（全部满足才置位）：
           - 工号声明有效
           - 部门声明明确属于本人（department_claim.subject == "self"）
           - 两项属于同一身份上下文（identity_epoch 一致）
           - 目录唯一命中
           - 工号、部门均与目录匹配

        安全约束：部门声明只信用户消息，绝不读取 issue.expected_department（LLM 可写）。
        核验过程记录到 state.identity_claim，便于追溯依据。
        """
        # 第一步：处理更正/撤回（必须在保存声明与 early-return 之前）
        self._process_identity_corrections(state)

        # 第二步：保存本人声明（无论目录是否已就绪）
        self._save_identity_claims(state)

        # 第三步：若已核验且声明仍有效，才沿用；否则继续比对
        if state.issue.verified_identity and state.has_valid_identity_verification():
            return

        u = state.user
        if not u:
            return  # 目录尚未就绪，声明已保存，待 lookup_user 后再核验

        claimed_eid = state.last_claimed_eid
        claim_obj = state.department_claim
        claimed_dept = claim_obj.value if claim_obj else ""

        dir_eid = (u.get("employee_id") or "").strip()
        dir_dept = (u.get("department") or "").strip().lower()

        # 部门声明有效性检查（Step 3）
        dept_claim_valid = (
            claim_obj is not None
            and claim_obj.subject == "self"
            and claim_obj.identity_epoch == state.identity_epoch
        )

        eid_match = bool(claimed_eid) and claimed_eid == dir_eid
        dept_match = dept_claim_valid and claimed_dept == dir_dept

        claim = {
            "source": "user_message",
            "claimed_employee_id": claimed_eid,
            "claimed_department": claimed_dept,
            "claimed_eid_source": state.last_claimed_eid_source,
            "claimed_dept_source": state.last_claimed_dept_source,
            "claimed_eid_turn": state.last_claimed_eid_turn,
            "claimed_dept_turn": state.last_claimed_dept_turn,
            "department_subject": claim_obj.subject if claim_obj else "",
            "department_source": claim_obj.source if claim_obj else "",
            "department_evidence": claim_obj.evidence_text if claim_obj else "",
            "department_epoch": claim_obj.identity_epoch if claim_obj else -1,
            "current_epoch": state.identity_epoch,
            "directory_employee_id": dir_eid,
            "directory_department": dir_dept,
            "eid_match": eid_match,
            "dept_match": dept_match,
        }
        state.identity_claim = claim

        # 双字段必须同时匹配：工号一致且部门一致才核验通过
        if eid_match and dept_match:
            state.mark_identity_verified(u)
            claim["result"] = "verified"
        else:
            claim["result"] = "unverified"
            reason = []
            if not claimed_eid:
                reason.append("未声明工号")
            elif not eid_match:
                reason.append(f"工号不匹配(声明{claimed_eid}≠目录{dir_eid})")
            if not claim_obj:
                reason.append("未声明本人部门")
            elif claim_obj.subject != "self":
                reason.append("部门声明非本人")
            elif claim_obj.identity_epoch != state.identity_epoch:
                reason.append("部门声明身份上下文不一致")
            elif not dept_match:
                reason.append(f"部门不匹配(声明{claimed_dept}≠目录{dir_dept})")
            claim["reason"] = "；".join(reason)

    @staticmethod
    def _auto_resolve_conflicts(state: SessionState, user_message: str) -> None:
        """用户直接观察消解已知冲突时，自动标记为 resolved。

        F04：LLM 有时忘记在 issue_update.resolved_conflicts 中声明，导致冲突
        永久 active，触发 outcome 规则 0 反复改写 need_info。此处作为安全网：
        若用户消息提到了冲突中的关键实体（如 SH-01/HZ-01）且包含确认/澄清用语，
        则认为该冲突已被用户直接观察消解。

        D04：疑问句不算观察 —— "现在能否帮我确认连的是SH-01还是HZ-01？"
        只是在请求确认，并未给出实际观察结果，不能据此消解冲突。
        """
        import re as _re
        if not user_message or not state.issue.conflicts:
            return
        msg_lower = user_message.lower()
        # 不确定性表述：用户尚未给出确定观察，不能据此消解冲突
        # 例如"现在仍无法确认"、"还不确定连的是哪个"——必须保持未裁决
        uncertainty_words = [
            "无法确认", "不确定", "不清楚", "不知道", "还没", "尚未",
            "可能", "也许", "大概", "似乎", "好像", "待确认", "需确认",
            "cannot confirm", "not sure", "not certain", "unclear",
            "don't know", "still checking",
        ]
        has_uncertainty = any(w in msg_lower for w in uncertainty_words)
        if has_uncertainty:
            return
        # D04：疑问句不算直接观察 —— "能否...确认"、"是否"、"吗"、"？"
        # 等句式表示用户在提问/请求确认，而非陈述观察结果。
        question_markers = ["能否", "是否", "吗", "么", "?", "？", "请帮我确认", "帮我确认"]
        is_question = any(m in user_message for m in question_markers)
        if is_question:
            return
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

    @staticmethod
    def _auto_close_answered_questions(state: SessionState, user_message: str) -> None:
        """用户消息已回答某疑点时自动关闭（S03 安全网）。

        LLM 有时忘记把已回答疑点移入 answered_questions，导致重复追问同一信息。
        判定条件（保守，避免误关）：
        - 用户消息是陈述句（非提问、非不确定表述）；
        - 疑点文本与用户消息共享关键实体（版本号 X.Y.Z、错误码、大写编号等）；
        - 用户消息包含动作/结果用语（确认、重装、重启、换网络、一样、不行…）。
        三者同时满足才关闭该疑点。
        """
        import re as _re
        if not user_message or not state.issue.unresolved_questions:
            return
        # 不确定表述：用户尚未给出确定观察，不能据此关闭疑点
        uncertainty_words = [
            "无法确认", "不确定", "不清楚", "不知道", "还没", "尚未",
            "可能", "也许", "大概", "似乎", "好像", "待确认", "需确认",
        ]
        if any(w in user_message for w in uncertainty_words):
            return
        # 疑问句不算回答
        question_markers = [
            "能否", "是否", "吗", "么", "?", "？", "请帮我确认",
            "帮我确认", "怎么", "如何", "为什么", "多少",
        ]
        if any(m in user_message for m in question_markers):
            return
        # 动作/结果用语：表明用户已执行或观察到结果
        action_words = [
            "确认", "重装", "重启", "升级", "换网络", "更换", "试过",
            "尝试", "可以", "好了", "不行", "一样", "仍然", "还是",
            "失败", "成功", "已", "刚",
        ]
        has_action = any(w in user_message for w in action_words)
        if not has_action:
            return
        # 提取关键实体：语义版本号 X.Y.Z、3 位以上数字（错误码/工号）、大写编号
        entity_re = _re.compile(r"\d+\.\d+\.\d+|\b\d{3,}\b|[A-Z]{1,5}-?\d{1,4}")
        user_entities = set(entity_re.findall(user_message))
        if not user_entities:
            return
        # Step 6：若疑点询问"认证哪一步/哪个阶段/原因"等需要解释性回答的问题，
        # 用户仅重复错误码/版本号不算已回答（N05：错误码重复只能证明错误仍存在）。
        explanatory_markers = ["哪一步", "哪个阶段", "为什么", "原因", "原理", "怎么回事", "认证"]
        for q in state.issue.unresolved_questions:
            if q.status != "open":
                continue
            q_entities = set(entity_re.findall(q.text))
            if q_entities and (q_entities & user_entities):
                # 共享实体但疑点要求解释性回答，且用户消息无解释性内容 → 不关闭
                needs_explanation = any(m in q.text for m in explanatory_markers)
                if needs_explanation:
                    explain_words = ["因为", "由于", "是在", "发生在", "阶段是", "步骤是"]
                    if not any(w in user_message for w in explain_words):
                        continue
                q.status = "answered"
        # 移除已回答的疑点，避免永久累加
        state.issue.unresolved_questions = [
            q for q in state.issue.unresolved_questions if q.status != "answered"
        ]

    # E05：日期年份清洗相关正则
    _YEARFUL_DATE_RE = None
    _MONTH_DAY_RE = None
    _INVENTED_YEAR_RE = None

    @classmethod
    def _ensure_date_regexes(cls):
        if cls._YEARFUL_DATE_RE is None:
            import re as _re
            cls._YEARFUL_DATE_RE = _re.compile(r"\d{4}[-/年.]\d{1,2}[-/月]\d{1,2}[日号]?")
            cls._MONTH_DAY_RE = _re.compile(r"(\d{1,2})[月/\-](\d{1,2})[日号]?")
            cls._INVENTED_YEAR_RE = _re.compile(
                r"(\d{4})[-/年.](\d{1,2})[-/月](\d{1,2})([日号]?)"
            )

    @classmethod
    def _yearless_dates_from_messages(cls, messages: List[str]) -> set:
        """从用户消息中收集缺少年份的 (月, 日) 集合。

        Step 6：若同一 (月, 日) 在任意用户消息中被明确给出了年份（如用户更正为
        "2027年10月28日"），则该 (月, 日) 不算缺年份日期，回复中的年份不予删除。
        不同问题的同一天也不能互相覆盖。
        """
        cls._ensure_date_regexes()
        # 收集所有带年份的 (月, 日) —— 这些是用户明确给出的，不删除年份
        yearful_pairs = set()
        for text in messages:
            for m in cls._INVENTED_YEAR_RE.finditer(text):
                mo, da = int(m.group(2)), int(m.group(3))
                if 1 <= mo <= 12 and 1 <= da <= 31:
                    yearful_pairs.add((mo, da))
        # 收集缺年份的 (月, 日)，排除已明确给过年份的
        pairs = set()
        for text in messages:
            cleaned = cls._YEARFUL_DATE_RE.sub(" ", text)
            for m in cls._MONTH_DAY_RE.finditer(cleaned):
                mo, da = int(m.group(1)), int(m.group(2))
                if 1 <= mo <= 12 and 1 <= da <= 31 and (mo, da) not in yearful_pairs:
                    pairs.add((mo, da))
        return pairs

    @classmethod
    def _strip_invented_years(cls, text: str, yearless_pairs: set) -> str:
        """把回复中被 LLM 自行补全年份的日期还原为缺年份的月日表达。"""
        if not text or not yearless_pairs:
            return text
        cls._ensure_date_regexes()

        def _repl(m):
            mo, da = int(m.group(2)), int(m.group(3))
            if (mo, da) in yearless_pairs:
                return f"{mo}月{da}日"
            return m.group(0)

        return cls._INVENTED_YEAR_RE.sub(_repl, text)

    @classmethod
    def _sanitize_fabricated_years(cls, state: SessionState, final: Dict[str, Any]) -> None:
        """E05：清洗回复与升级摘要中被 LLM 自行补全的年份。

        用户提供的日期若缺少年份，回复和升级摘要必须原样保留，不得自行补全年份。
        此处作为代码兜底：收集用户消息中缺年份的 (月,日)，将 final.user_message
        与 final.escalation.summary 中匹配的带年份日期还原为月日表达。
        """
        user_msgs = [
            m.get("content", "") for m in state.messages if m.get("role") == "user"
        ]
        yearless = cls._yearless_dates_from_messages(user_msgs)
        if not yearless:
            return
        reply = final.get("user_message", "")
        if isinstance(reply, str) and reply:
            final["user_message"] = cls._strip_invented_years(reply, yearless)
        esc = final.get("escalation")
        if isinstance(esc, dict):
            summary = esc.get("summary", "")
            if isinstance(summary, str) and summary:
                esc["summary"] = cls._strip_invented_years(summary, yearless)

    # ---- 统一最终决议（Step 1：唯一事实源）----

    @staticmethod
    def _detect_completion_basis(state: SessionState, final: Dict[str, Any]) -> str:
        """判断当前问题是否有真实的"完成依据"。

        不能仅凭疑点列表为空或模型置信度高就判定完成。完成依据仅限：
        - action_executed：本轮有真实成功的 execute_action 回执
        - user_confirmed：用户最新消息明确表示已恢复/已解决
        - answer_delivered：纯咨询类问题，模型已交付答案且无需执行动作
        """
        # 1) 本轮真实成功执行的动作
        has_real_action = any(
            r.turn == state.turn_count and r.name == "execute_action" and r.status == "ok"
            for r in state.tool_records
        )
        if has_real_action:
            return "action_executed"
        # 2) 用户明确确认恢复。扩展词表覆盖"可以登录/能连上/恢复正常"等表达，
        #    并按问题类别补充连接类（VPN）或登录类（密码/账号）恢复词。
        #    否定拦截：若消息含"还不能/仍然不能/没好/未恢复"等否定恢复表达，
        #    则无完成依据（用户明确说未恢复），同时阻断 user_confirmed 与
        #    answer_delivered——只有真实动作回执(action_executed)可作为依据。
        user_msg = AgentLoop._latest_user_message(state)
        negation_recovery = [
            "还不能", "仍然不能", "依旧不能", "还是不能", "还是不行",
            "没好", "未恢复", "尚未恢复", "仍未恢复", "依旧不行", "还是断",
            "仍然断", "还是连不上", "还是登不上",
        ]
        has_negation = any(w in user_msg for w in negation_recovery)
        if has_negation:
            return ""
        confirm_words = [
            "好了", "能用了", "可以了", "已解决", "解决了", "恢复了", "正常了", "搞定了",
            "已经好了", "已经可以", "恢复正常", "没问题了",
        ]
        category = (state.issue.category or "").lower()
        if category in ("vpn", "network", "wifi"):
            confirm_words += [
                "可以登录", "可以访问", "可以连接", "能连上", "连得上", "连上了",
                "能访问", "登得上", "能登录", "已经连上",
            ]
        elif category in ("password", "account"):
            confirm_words += [
                "可以登录", "登得上", "能登录", "密码能用", "已经能登",
            ]
        if any(w in user_msg for w in confirm_words):
            return "user_confirmed"
        # 3) 纯咨询交付答案：模型声明 resolved，且无需身份类动作
        candidate = final.get("state")
        if candidate == "resolved":
            needs_action = any(
                isinstance(a, dict) and a.get("action") in IDENTITY_ACTIONS
                for a in (final.get("actions_taken") or [])
            )
            if not needs_action and not has_real_action:
                return "answer_delivered"
        return ""

    def _compute_final_decision(
        self, final: Dict[str, Any], state: SessionState
    ) -> "FinalDecision":
        """依据业务事实计算统一的最终决议（模型 state 仅为候选）。

        对外状态判定规则：
        - escalated：候选为 escalated（已通过 outcome 审查）
        - resolved：所有问题都有完成依据且无待补充信息/待执行步骤
        - pending_user：已提供步骤等待用户反馈，或有完成依据但仍有未解决疑点
        - need_info：仍有问题需要用户补充信息
        """
        candidate = final.get("state", "need_info")
        conf = final.get("confidence", 0.5)
        try:
            conf = float(conf) if conf is not None else 0.5
        except (TypeError, ValueError):
            conf = 0.5

        # 跨所有问题收集未决疑点（不只看活动问题）：任一问题有 open 疑点，
        # 整场会话都不能判 resolved（如密码已重置但 VPN 仍有未决疑点）。
        open_questions: List[str] = []
        for iss in state.issues.values():
            for q in iss.unresolved_questions:
                if q.status == "open":
                    open_questions.append(q.text)
        completion_basis = self._detect_completion_basis(state, final)

        # 候选 escalated 直接采纳（outcome 已审查升级合法性）
        if candidate == "escalated":
            decision_state = "escalated"
        elif candidate == "resolved":
            if completion_basis and not open_questions:
                decision_state = "resolved"
            elif completion_basis and open_questions:
                # 有完成依据但仍有未解决疑点（如密码已重置但 VPN 未恢复）
                decision_state = "pending_user"
            elif open_questions:
                decision_state = "need_info"
            else:
                # 无完成依据也无待补充信息 → 保守追问
                decision_state = "need_info"
        elif candidate == "pending_user":
            decision_state = "pending_user"
        else:  # need_info
            decision_state = "need_info"

        # 多问题聚合兜底：若活动问题已 resolved，但存在其他非 resolved 问题
        # （有未决疑点或状态非 resolved），整场会话降级为 pending_user，
        # 避免"一个问题解决就关闭整个会话"。
        if decision_state == "resolved":
            for iss in state.issues.values():
                if iss.issue_id == state.issue.issue_id:
                    continue
                has_open = any(q.status == "open" for q in iss.unresolved_questions)
                if iss.status != "resolved" or has_open:
                    decision_state = "pending_user"
                    break

        # 本轮真实动作回执（供回复与审计引用）
        receipt_ids: List[str] = []
        for r in state.tool_records:
            if (
                r.turn == state.turn_count
                and r.name == "execute_action"
                and r.status == "ok"
            ):
                action = (r.args or {}).get("action", "")
                receipt_ids.append(f"{action}:{state.turn_count}")

        # 待执行步骤：pending_user 时从回复中提取（保守取非空）
        next_steps: List[str] = []
        if decision_state == "pending_user":
            next_steps = ["见回复中的操作步骤"]

        # 交接信息
        handoff = None
        if decision_state == "escalated":
            esc = final.get("escalation") or {}
            handoff = {
                "reason": esc.get("reason", "unspecified"),
                "urgency": esc.get("urgency", "medium"),
                "summary": esc.get("summary", ""),
            }

        # 为每个问题生成 IssueResult：活动问题用本轮计算状态，其余问题用已存储状态。
        # 保证 trace/审计可追溯多问题各自的状态与完成依据。
        issue_results: List[IssueResult] = []
        for iss in state.issues.values():
            if iss.issue_id == state.issue.issue_id:
                ir_status = decision_state
                ir_basis = completion_basis
            else:
                ir_status = iss.status or "open"
                ir_basis = iss.completion_basis or ""
            issue_results.append(IssueResult(
                issue_id=iss.issue_id or state.session_id,
                category=iss.category,
                status=ir_status,
                completion_basis=ir_basis,
                goal=iss.description,
            ))

        # Phase 3：回复一致性强制层——事实性状态由 FinalDecision 确定性生成，
        # 模型文本仅保留诊断解释部分，避免"状态 pending_user 但回复说已解决"。
        raw_reply = final.get("user_message", "") or ""
        final_reply = self._enforce_reply_consistency(
            raw_reply, decision_state, issue_results, open_questions,
        )

        return FinalDecision(
            state=decision_state,
            issue_results=issue_results,
            receipt_ids=receipt_ids,
            next_questions=open_questions,
            next_steps=next_steps,
            handoff=handoff,
            confidence=conf,
            reply=final_reply,
            evidence=[e for e in (final.get("evidence") or []) if isinstance(e, str)],
        )

    @staticmethod
    def _enforce_reply_consistency(
        reply: str,
        decision_state: str,
        issue_results: List["IssueResult"],
        open_questions: List[str],
    ) -> str:
        """根据最终决策状态强制回复措辞一致。

        原则：
        - 事实性状态（是否解决、还有哪些未决）由代码决定，不由模型措辞决定。
        - 模型的诊断解释内容保留，但与状态矛盾的"已解决"类伪声明必须剔除。
        - pending_user 且有未决疑点时，确定性追加状态说明句，使用完整句子而非删短语。
        """
        import re as _re
        if not reply:
            reply = ""

        # 1) 非 resolved 状态：剔除模型伪造的"已解决"类整体完成声明。
        #    只删整体完成声明，不删具体动作回执（如"已为你重置密码"是真实结果）。
        if decision_state != "resolved":
            false_resolution_patterns = [
                r"问题已解决", r"已全部解决", r"全部解决", r"已搞定",
                r"已为你搞定", r"已处理完毕", r"已修复", r"已完成处理",
                r"问题已搞定", r"均已解决", r"已全部处理",
            ]
            for pat in false_resolution_patterns:
                reply = _re.sub(pat, "", reply)
            # 清理删除后残留的标点/空白
            reply = _re.sub(r"[，；;]\s*[，；;]", "，", reply)
            reply = _re.sub(r"^[，；;。.\s]+", "", reply)
            reply = _re.sub(r"\s{2,}", " ", reply)

        # 2) pending_user 且有未决疑点：确定性追加状态说明句（完整句子），
        #    明确告知用户哪些事项尚未完成，避免与"已解决"措辞矛盾。
        if decision_state == "pending_user" and open_questions:
            pending_items = "；".join(open_questions[:3])
            status_line = f"目前仍有未解决事项需确认：{pending_items}。"
            if reply:
                reply = f"{reply.rstrip('。.；;')}；{status_line}"
            else:
                reply = status_line

        # 3) 多问题状态展示：若存在多个问题且状态不一致，追加各问题状态摘要。
        #    例如"密码已重置；VPN 尚未恢复"——区分不同问题的完成情况。
        if decision_state != "escalated" and len(issue_results) > 1:
            resolved_issues = [ir for ir in issue_results if ir.status == "resolved"]
            open_issues = [ir for ir in issue_results if ir.status != "resolved"]
            if resolved_issues and open_issues:
                resolved_names = "、".join(
                    (ir.category or "问题") for ir in resolved_issues[:3]
                )
                open_names = "、".join(
                    (ir.category or "问题") for ir in open_issues[:3]
                )
                summary = f"{resolved_names}已处理；{open_names}尚未恢复。"
                if reply:
                    reply = f"{reply.rstrip('。.；;')}；{summary}"
                else:
                    reply = summary

        return reply.strip()

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
        self, action: str, params: Dict[str, Any], state: SessionState,
        call_id: str = "", source: str = "native",
    ):
        """统一动作执行入口。

        执行前由 actions.py 完成需求(白名单)、身份、目标绑定、资源白名单校验。
        返回 (full_result, biz_ok, data, clause)：
        - full_result: registry 原始返回（供回填消息历史）
        - biz_ok: 业务是否成功（transport ok 且 data.status == ok）
        - data: 动作 data 部分
        - clause: 失败原因（策略条款或传输错误）

        所有路径（原生执行 / 身份补偿重试 / 对账补执行）均经过此入口，
        并通过 tracer.log_execution 留下统一的可追溯记录（Step 7）。
        """
        tc = ToolCall(
            id=call_id or f"act-{action}", name="execute_action",
            arguments={"action": action, "params": params},
        )
        result = self.registry.safe_execute(tc, ToolContext(settings=self.settings, state=state))
        transport_ok = result.get("status") == "ok"
        # Step 4：超时写操作记为"结果未知"，不能当成失败后直接再次执行。
        timeout_err = str(result.get("error", "")).lower()
        is_timeout = (not transport_ok) and any(
            kw in timeout_err for kw in ("timeout", "timed out", "超时", "deadline")
        )
        data = result.get("data", result) if transport_ok else {}
        if not isinstance(data, dict):
            data = {}
        biz_ok = transport_ok and data.get("status") == "ok"
        clause = ""
        if not biz_ok and not is_timeout:
            clause = str(data.get("policy_clause", ""))
            if not clause and not transport_ok:
                clause = str(result.get("error", "transport_failure"))
        # 统一执行日志：关联会话/问题/轮次/执行标识/业务结果
        if self.tracer is not None:
            p = params or {}
            target = p.get("employee_id") or (state.verified_subject or {}).get("employee_id", "")
            if action == "grant_standard_software":
                resource = str(p.get("software", ""))
            else:
                resource = ""
            # Step 7：result_reference 只含执行标识，不含秘密值（临时密码等）
            biz_status = "ok" if biz_ok else ("unknown" if is_timeout else ("denied" if clause else "error"))
            res_ref = tc.id  # 用执行标识代替秘密值引用
            self.tracer.log_execution(
                session_id=state.session_id,
                issue_id=state.issue.issue_id or state.session_id,
                turn=state.turn_count,
                execution_id=tc.id,
                action=action,
                target=str(target),
                resource=resource,
                business_status=biz_status,
                result_reference=res_ref,
                source=source,
            )
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
            res = r.get("result")
            # 防御：result 可能不是 dict（LLM/工具返回畸形结构），不能崩溃
            if not isinstance(res, dict):
                res = {}
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

    @staticmethod
    def _sanitize_fabricated_credentials(
        msg: str, ok_results: List[Dict[str, Any]]
    ) -> str:
        """从 LLM 原回复中剔除无法核实的凭证声明短语（Step 4）。

        W03：模型在 pending_user 回复中夹带伪造的临时密码（如 Eval-FAKE-ONLY），
        直接拼接会让真实密码与伪造密码同时出现。

        Step 4：不再按 [；;。.，,] 拆分整条回复（会破坏 7.2.3 版本号、网址、文件名），
        改为用正则精准定位并删除"凭证声明短语 + 紧随的凭证值"，保留其余诊断内容。
        凭证声明关键词与 D05 对账保持一致，避免漏判。
        """
        if not msg:
            return ""
        # 收集真实执行过的动作类型
        real_actions = set()
        for r in ok_results:
            if isinstance(r, dict):
                a = r.get("action", "")
                if a:
                    real_actions.add(a)
        # 凭证声明短语 → 动作类型；仅当该动作有真实执行记录时才删除原回复中的声明
        # （真实凭证已由回复生成器提供，原回复中的同类声明可能是伪造）
        claim_patterns = [
            # (正则, 动作类型) —— 匹配声明短语及其后可能跟随的凭证值
            (r"(?:密码已重置|已重置密码|已为你重置(?:密码)?|临时密码[：:是\s]*)[^，。；;\s\"']*",
             "reset_password"),
            (r"(?:账号已解锁|已解锁(?:账号)?|已为你解锁(?:账号)?)", "unlock_account"),
            (r"(?:已授予(?:权限)?|权限已开通|已开通(?:权限)?)", "grant_standard_software"),
        ]
        import re as _re
        result = msg
        for pattern, action in claim_patterns:
            if action in real_actions:
                # 删除该类凭证声明短语（含紧随的凭证值），保留前后内容
                result = _re.sub(pattern, "", result)
        # 清理删除后留下的多余标点和空白
        result = _re.sub(r"[，；;]\s*[，；;]", "，", result)
        result = _re.sub(r"\s{2,}", " ", result)
        return result.strip(" ，；;。")

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
        # Step 4：仅当核验仍有效时才重试（部门声明被撤回则不重试）
        if not state.has_valid_identity_verification():
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
            _, biz_ok, data, clause = self._run_action(
                action, params, state, call_id=f"retry-{action}", source="identity_retry",
            )
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
        - 补执行被拒或传输失败 → 记录被拒动作，不丢失已成功的动作结果（E04）
        - 部分成功部分被拒 → 回复同时包含成功结果和被拒说明，state=escalated
        - 全部成功 → 依据真实结果重新生成回复
        - G03：本轮有被拒的 execute_action 且未重试成功时，不允许 state=resolved 声称成功
        """
        # 保存 LLM 原始回复（reply rebuild 后会被覆盖），用于 D05 凭证声明对账。
        # Y03：回复声称"账号已解锁"但实际只执行了 reset_password，必须在 rebuild
        # 前捕获原始声明，避免 rebuild 后丢失声明信息。
        original_reply = final.get("user_message") or ""

        declared = [a for a in (final.get("actions_taken") or []) if isinstance(a, dict)]
        declared = [a for a in declared if a.get("action") not in READONLY_TOOLS]

        # 已成功执行的动作集合（按完整资源去重键，含历史轮次，用于幂等去重）
        executed_ok = set()
        for r in state.tool_records:
            if r.name == "execute_action" and r.status == "ok":
                key = self._action_dedup_key(
                    (r.args or {}).get("action", ""), (r.args or {}).get("params") or {}, state
                )
                if key[0]:
                    executed_ok.add(key)

        # 本轮所有成功执行的动作 data（用于生成回复，仅限本轮，避免把上一问题的
        # 成功动作混入当前问题的回复；也覆盖 LLM 漏写 actions_taken 的场景）。
        executed_this_turn_data: List[Dict[str, Any]] = []
        for r in state.tool_records:
            if r.turn == state.turn_count and r.name == "execute_action" and r.status == "ok":
                if isinstance(r.data, dict):
                    executed_this_turn_data.append(r.data)

        denied_clause = ""
        denied_action = ""
        ok_results: List[Dict[str, Any]] = []
        # seen_actions 使用完整幂等键（动作+目标+资源），避免不同资源被错误去重。
        # W05：grant_standard_software 的 jira 与 grafana_readonly 必须都进入回执。
        seen_actions = set()
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
                    seen_actions.add(key)
                continue
            _, biz_ok, data, clause = self._run_action(
                name, params, state, call_id=f"reconcile-{name}", source="reconcile",
            )
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
                seen_actions.add(key)
            else:
                denied_clause = denied_clause or clause
                denied_action = denied_action or name

        # 补齐：本轮成功执行但 LLM 未在 actions_taken 中声明的动作，也纳入回复。
        # 避免漏写声明时 LLM 原回复中的伪造密码不被真实密码替换（issue #2）。
        # W05：去重键必须包含资源（software），否则同一目标的 jira 与 grafana_readonly
        # 会被 (action, target) 去重掉其中一个，导致回执只列出一个资源。
        for data in executed_this_turn_data:
            if not isinstance(data, dict):
                continue
            d_action = data.get("action", "")
            d_target = data.get("target", "")
            d_res = data.get("result") if isinstance(data.get("result"), dict) else {}
            if d_action == "grant_standard_software":
                d_resource = str(d_res.get("granted", "")).strip().lower()
            else:
                d_resource = ""
            dkey = (d_action, d_target, d_resource)
            if d_action and dkey not in seen_actions:
                ok_results.append(data)
                seen_actions.add(dkey)

        # G03：收集本轮所有被拒且未重试成功的 execute_action（含未声明的）
        denied_this_turn = [
            r for r in state.tool_records
            if r.turn == state.turn_count and r.name == "execute_action"
            and r.status == "error"
        ]
        has_denied = bool(denied_clause) or bool(denied_this_turn)

        # 有被拒动作 → 不能声称全部成功
        if has_denied:
            # 判断是否所有被拒动作都是身份原因（缺身份/身份不匹配）。
            # 若是，不立即升级，交由 outcome 规则 2 追问身份信息（设置 pending）；
            # 若有任何非身份原因（策略/白名单/目标不匹配/传输失败），才升级。
            all_identity = True
            if denied_clause and "identity" not in denied_clause.lower():
                all_identity = False
            for r in denied_this_turn:
                s = (r.summary or "")
                if "identity" not in s.lower() and "身份" not in s:
                    all_identity = False
                    break
            if not all_identity:
                reason = (
                    "policy_forbidden" if denied_clause and any(
                        k in denied_clause.lower() for k in ("policy", "condition", "whitelist", "forbidden"))
                    else "action_failed"
                )
                esc = dict(final)
                esc["state"] = "escalated"
                esc["escalation"] = dict(esc.get("escalation") or {})
                esc["escalation"]["reason"] = reason
                if denied_action:
                    esc["escalation"]["forbidden_action"] = denied_action
                # E04：部分成功时，回复同时包含已完成的动作结果和被拒说明
                success_reply = self._build_reply_from_results(ok_results) if ok_results else ""
                denied_names = sorted(set(
                    (r.args or {}).get("action", "") for r in denied_this_turn
                    if (r.args or {}).get("action")
                ))
                denied_detail = denied_clause.split(":")[-1] if ":" in denied_clause else (denied_clause or "需人工审批")
                denied_msg = (
                    f"以下操作无法自动完成（{denied_detail}），已转人工处理："
                    + "、".join(denied_names)
                ) if denied_names else f"部分操作无法完成（{denied_detail}），已转人工处理。"
                esc["user_message"] = (success_reply + "\n" + denied_msg) if success_reply else denied_msg
                return self.outcome.complete_summary(esc, state, force_reason=reason)
            # 全部被拒都是身份原因：不升级，保留 final 原状态（resolved/pending_user），
            # 交由 outcome 规则 2 追问缺失的身份信息并设置 pending_identity_question。
            # 回复中的伪造成功声明由 D05/规则 2 重写覆盖。

        # 全部成功：依据真实结果重新生成回复（禁止仅追加密码）
        if ok_results:
            # D06：LLM 已显式置 state=escalated（如部分权限需审批）时，
            # 不能覆盖回 resolved；但必须用真实执行结果重写回复中的凭证部分，
            # 防止 LLM 伪造临时密码（X04：Eval-FAKE-ONLY 必须被真实密码替换）。
            if final.get("state") != "escalated":
                reply = self._build_reply_from_results(ok_results)
                if reply:
                    final = dict(final)
                    orig_state = final.get("state")
                    if orig_state == "resolved":
                        # S02：若仍有未解决疑点（如 VPN 待验证），保留模型原回复中的
                        # 继续诊断内容，只在前面拼接真实动作回执，不覆盖为纯回执。
                        # 否则（无未解决疑点）用真实结果重写回复。
                        has_open_questions = any(
                            q.status == "open" for q in state.issue.unresolved_questions
                        )
                        if has_open_questions:
                            orig_msg = final.get("user_message", "") or ""
                            sanitized = self._sanitize_fabricated_credentials(orig_msg, ok_results)
                            base = reply.rstrip("。.；;")
                            if sanitized:
                                final["user_message"] = f"{base}；{sanitized}"
                            else:
                                final["user_message"] = base
                        else:
                            final["user_message"] = reply
                        final["confidence"] = 0.9
                    else:
                        # need_info / pending_user：保留模型继续诊断的意图，
                        # 仅把已成功动作的回执拼入回复，不能覆盖为 resolved。
                        # S02：密码重置成功但 VPN 仍未解决，模型返回 need_info
                        # 继续排查 VPN，此时若强制 resolved 会丢弃 VPN 问题。
                        # W03：原回复可能夹带伪造密码/授权/解锁声明，必须剔除，
                        # 只保留真实回执 + 纯诊断内容。
                        orig_msg = final.get("user_message", "") or ""
                        base = reply.rstrip("。.；;")
                        if orig_msg:
                            sanitized = self._sanitize_fabricated_credentials(orig_msg, ok_results)
                            if sanitized:
                                final["user_message"] = f"{base}；{sanitized}"
                            else:
                                final["user_message"] = base
                        else:
                            final["user_message"] = reply
            else:
                # X04：state=escalated 且有成功执行的凭证类动作时，
                # 用真实结果替换 LLM 可能伪造的密码/凭据，同时保留升级上下文。
                reply = self._build_reply_from_results(ok_results)
                if reply:
                    final = dict(final)
                    # 拼接升级上下文：优先用 unresolved_questions，其次 escalation.summary。
                    # 避免与已有"人工"措辞重复。
                    unresolved = (final.get("issue_update") or {}).get("unresolved_questions") or []
                    if isinstance(unresolved, list) and unresolved:
                        esc_parts = [
                            (q.get("text") if isinstance(q, dict) else q)
                            for q in unresolved if q
                        ]
                        # Z02：text 可能是非字符串（如 int 42），强制转字符串并过滤空值，
                        # 避免 "；".join 时 TypeError: expected str instance, int found
                        esc_parts = [str(p) for p in esc_parts if p not in (None, "")]
                        unresolved_text = "；".join(esc_parts)
                        if unresolved_text:
                            esc_note = (
                                f"未解决：{unresolved_text}，已转人工处理。"
                                if "人工" not in unresolved_text
                                else f"未解决：{unresolved_text}。"
                            )
                        else:
                            esc_note = "部分操作已转人工处理。"
                    else:
                        summary = (final.get("escalation") or {}).get("summary", "")
                        esc_note = summary if summary else "部分操作已转人工处理。"
                    # 拼接：去掉 reply 末尾标点再加分隔，避免 "。；" 之类
                    base = reply.rstrip("。.；;")
                    final["user_message"] = f"{base}；{esc_note}"

        # D05：回复中声明的凭证类动作必须有对应的成功执行记录（任意轮次）。
        # 按动作类型逐项核对，而非简单看"本轮有无成功执行"：
        # - Y03：只执行 reset_password 却声称"账号已解锁" → unlock_account 无执行记录 → 升级
        # - Y04：复述上轮 reset_password 成功结果 → reset_password 有历史执行记录 → 允许
        # - X03：上轮 reset_password 不能证明本轮 unlock_account → 按动作类型核对，不混淆
        # - W02：上轮为 E1001 重置不能证明本轮为 E1002 重置 → 必须同时核对目标员工
        if final.get("state") == "resolved":
            # 回复中的凭证声明关键词 → 对应动作类型
            claim_to_action = [
                (["密码已重置", "已重置密码", "已为你重置", "临时密码"], "reset_password"),
                (["账号已解锁", "已解锁", "已为你解锁"], "unlock_account"),
                (["已授予", "权限已开通", "已开通"], "grant_standard_software"),
            ]
            # 收集所有成功执行过的动作（含目标员工，任意轮次）
            executed_actions: List[tuple] = []  # [(action_name, employee_id)]
            for r in state.tool_records:
                if r.name == "execute_action" and r.status == "ok":
                    aname = (r.args or {}).get("action", "")
                    eid = ((r.args or {}).get("params") or {}).get("employee_id", "")
                    if aname:
                        executed_actions.append((aname, eid))
            # 从回复中提取目标员工（如有）：若回复点名了某员工，则必须有该员工的执行记录
            reply_eid = _extract_employee_id(original_reply)
            claims_without_execution = []
            has_valid_verif = state.has_valid_identity_verification()
            for keywords, action in claim_to_action:
                if any(kw in original_reply for kw in keywords):
                    # 身份类动作在核验未通过时本就不会被执行（actions.py 闸门），
                    # 此时回复中的成功声明是 LLM 误报，交由 outcome 规则 2 追问身份，
                    # 不视为"执行失败"而升级。
                    if action in IDENTITY_ACTIONS and not has_valid_verif:
                        continue
                    has_match = False
                    for aname, eid in executed_actions:
                        if aname == action:
                            if reply_eid:
                                # 回复指定了员工 → 必须是该员工的执行记录
                                if eid == reply_eid:
                                    has_match = True
                                    break
                            else:
                                # 回复未指定员工 → 任意目标的执行记录均可（兼容复述历史结果）
                                has_match = True
                                break
                    if not has_match:
                        claims_without_execution.append(action)
            if claims_without_execution:
                esc = dict(final)
                esc["state"] = "escalated"
                esc["escalation"] = dict(esc.get("escalation") or {})
                esc["escalation"]["reason"] = "action_failed"
                esc["escalation"]["unexecuted_claims"] = claims_without_execution
                # 保留已成功执行动作的真实回执，避免用户完全收不到结果
                success_reply = self._build_reply_from_results(ok_results) if ok_results else ""
                base = success_reply.rstrip("。.；;") if success_reply else ""
                esc["user_message"] = (
                    f"{base}；部分操作未能实际执行，已转人工 IT 支持处理。"
                    if base
                    else "抱歉，刚才的操作未能实际执行成功，我已经把情况转给人工 IT 支持，他们会尽快为你处理。"
                )
                return self.outcome.complete_summary(esc, state, force_reason="action_failed")
        return final

    def _build_turn_result(
        self, state: SessionState, decision: FinalDecision,
        turn_tools: List[Dict[str, Any]], latency_ms: int,
    ) -> TurnResult:
        """从统一 FinalDecision 构造对外响应，确保 API 状态与决议一致。"""
        esc = None
        if decision.state == "escalated":
            esc = decision.handoff or {}
            if state.escalation_ticket:
                esc = {**esc, "ticket_id": state.escalation_ticket["id"]}
        usage = {"prompt": state.total_usage["prompt"], "completion": state.total_usage["completion"]}
        return TurnResult(
            session_id=state.session_id,
            reply=decision.reply,
            state=decision.state,
            confidence=decision.confidence,
            evidence=list(decision.evidence),
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
