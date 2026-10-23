"""会话状态：双轨记忆。

- 原始消息历史（messages）：OpenAI 格式，隐式记忆，构建提示时只取最近 N 条
- 结构化问题卡（IssueContext）+ 假设列表 + 工具记录：显式记忆，
  每轮注入系统提示，使第 15 轮的输入长度仍然稳定（成本/延迟可控）

显式状态是"跨轮次追踪已调查内容和未解决问题"的直接实现，
也是升级摘要的数据来源 —— 摘要不依赖 LLM 回忆，从状态卡合成。
"""
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


@dataclass
class IssueContext:
    """结构化问题卡（LLM 每轮通过 final.issue_update 维护）。"""

    category: str = "unknown"  # password | vpn | access | account | other
    description: str = ""
    user_id: str = ""
    verified_identity: bool = False
    affected_service: str = ""
    symptoms: List[str] = field(default_factory=list)
    attempted: List[str] = field(default_factory=list)
    unresolved_questions: List[str] = field(default_factory=list)
    conflicts: List[str] = field(default_factory=list)  # 数据源矛盾点，如 "kb: 建议重启 vs system_status: 维护中"

    def merge(self, update: Dict[str, Any]) -> None:
        """用 LLM 给的增量更新合并问题卡（None/缺省字段不覆盖）。"""
        if not isinstance(update, dict):
            return
        for key in ("category", "description", "user_id", "affected_service"):
            v = update.get(key)
            if isinstance(v, str) and v:
                setattr(self, key, v)
        if isinstance(update.get("verified_identity"), bool):
            # 身份核验只能由代码在工具执行后置位，这里只接受 LLM 的"已核验"声明做交叉，
            # 但最终以 lookup_user + 比对逻辑为准（见 loop.py 的 _maybe_verify_identity）
            self.verified_identity = self.verified_identity or update["verified_identity"]
        for key in ("symptoms", "attempted", "unresolved_questions", "conflicts"):
            v = update.get(key)
            if isinstance(v, list):
                merged = getattr(self, key)
                for item in v:
                    if isinstance(item, str) and item and item not in merged:
                        merged.append(item)

    def to_prompt(self) -> str:
        verified = "是" if self.verified_identity else "否"
        return (
            f"类别={self.category} | 描述={self.description or '（待补充）'} | "
            f"用户={self.user_id or '未知'} | 身份核验={verified} | "
            f"影响服务={self.affected_service or '未知'} | "
            f"症状={self.symptoms or '无'} | 已尝试={self.attempted or '无'} | "
            f"未解决疑点={self.unresolved_questions or '无'} | "
            f"证据冲突={self.conflicts or '无'}"
        )


@dataclass
class Hypothesis:
    """一条诊断假设及其验证状态。"""

    text: str
    status: str = "testing"  # testing | confirmed | rejected | conflicted
    evidence: List[str] = field(default_factory=list)


@dataclass
class ToolRecord:
    """一次工具调用的压缩记录（全量结果太长，提示里只注入摘要）。"""

    turn: int
    name: str
    args: Dict[str, Any]
    status: str  # ok | error
    summary: str  # 结果压缩摘要


@dataclass
class SessionState:
    """单个会话的全部状态。"""

    session_id: str
    created_at: datetime = field(default_factory=datetime.now)
    messages: List[Dict[str, Any]] = field(default_factory=list)  # OpenAI 格式全量历史
    issue: IssueContext = field(default_factory=IssueContext)
    hypotheses: List[Hypothesis] = field(default_factory=list)
    tool_records: List[ToolRecord] = field(default_factory=list)
    user: Optional[Dict[str, Any]] = None  # lookup_user 命中的用户记录（身份上下文）
    clarified_gaps: List[str] = field(default_factory=list)  # 已追问过的信息缺口
    turn_count: int = 0
    consecutive_need_info: int = 0
    consecutive_tool_errors: int = 0
    identity_ask_count: int = 0
    final_retry_used: bool = False  # 每轮重置
    last_clarify_turn: int = -99  # 最近一次"改写为追问"发生的轮次（检测"无新证据"用）
    escalated: bool = False
    resolved: bool = False
    escalation_ticket: Optional[Dict[str, Any]] = None
    total_usage: Dict[str, int] = field(default_factory=lambda: {"prompt": 0, "completion": 0})

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

    def apply_final(self, final: Dict[str, Any]) -> None:
        """把通过审查的最终结论落到状态卡。"""
        self.issue.merge(final.get("issue_update", {}))
        for h in (final.get("issue_update", {}) or {}).get("hypotheses", []):
            if isinstance(h, dict) and h.get("text"):
                self._upsert_hypothesis(h)
        state = final.get("state")
        if state == "escalated":
            self.escalated = True
        elif state == "resolved":
            self.resolved = True

    def _upsert_hypothesis(self, h: Dict[str, Any]) -> None:
        for existing in self.hypotheses:
            if existing.text == h["text"]:
                existing.status = h.get("status", existing.status)
                for ev in h.get("evidence", []):
                    if ev not in existing.evidence:
                        existing.evidence.append(ev)
                return
        self.hypotheses.append(
            Hypothesis(
                text=h["text"],
                status=h.get("status", "testing"),
                evidence=[e for e in h.get("evidence", []) if isinstance(e, str)],
            )
        )

    # ---- 序列化（供 API 与评测报告使用）----

    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at.isoformat(),
            "turn_count": self.turn_count,
            "issue": self.issue.__dict__.copy(),
            "hypotheses": [h.__dict__.copy() for h in self.hypotheses],
            "tool_records": [r.__dict__.copy() for r in self.tool_records],
            "user": self.user,
            "escalated": self.escalated,
            "resolved": self.resolved,
            "escalation_ticket": self.escalation_ticket,
            "total_usage": self.total_usage,
            "message_count": len(self.messages),
        }


def json_dumps(obj: Any) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False)


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]
