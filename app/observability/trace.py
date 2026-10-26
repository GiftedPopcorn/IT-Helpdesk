"""TraceLogger：JSONL 结构化运行日志（可观测性）。

每轮追加一条记录：轮次、结论、工具调用链、token 用量、延迟、
业务拒绝动作、对账补执行动作。所有凭据（临时密码等）自动脱敏。
"""
import json
import os
import re
from typing import Any, Dict, Optional


# 凭据脱敏：临时密码 / token 等敏感值替换为 ***
_CREDENTIAL_PATTERNS = [
    re.compile(r"Temp-\d{3,6}", re.IGNORECASE),
    # 仅匹配键值结构（带冒号），避免误命中类别名 "password" 后跟管道符等场景
    re.compile(r"(temporary_password[\"']?\s*:\s*[\"']?)[^\"',}\s]+", re.IGNORECASE),
    re.compile(r"(password[\"']?\s*:\s*[\"']?)[^\"',}\s]+", re.IGNORECASE),
    # 中文自然语言中的临时密码："临时密码：xxx"、"临时密码是xxx"、"临时密码 xxx"
    re.compile(r"(临时密码[：:是\s]+)[^，。\s\"',}]+", re.IGNORECASE),
]


def _mask(value: Any) -> Any:
    """递归脱敏：字符串中的凭据替换为 ***；dict/list 递归处理。"""
    if isinstance(value, str):
        masked = value
        for pat in _CREDENTIAL_PATTERNS:
            masked = pat.sub(lambda m: m.group(1) + "***" if m.groups() else "***", masked)
        return masked
    if isinstance(value, dict):
        return {k: _mask(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_mask(v) for v in value]
    return value


class TraceLogger:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def log_turn(
        self,
        state,
        final: Dict[str, Any],
        verdict,
        result,
    ) -> None:
        # 业务拒绝动作（execute_action 被策略拦截的）
        denied_actions = []
        reconciled_actions = []
        for r in state.tool_records:
            if r.turn != state.turn_count:
                continue
            if r.name == "execute_action" and r.status == "error":
                denied_actions.append({
                    "action": (r.args or {}).get("action"),
                    "clause": (r.summary or "")[:120],
                })
            elif r.name == "execute_action" and "重试" in (r.summary or ""):
                reconciled_actions.append({
                    "action": (r.args or {}).get("action"),
                    "result": r.summary[:80],
                })

        record: Dict[str, Any] = {
            "ts": _now(),
            "session_id": state.session_id,
            "turn": state.turn_count,
            "issue_id": getattr(state.issue, "id", None) or state.session_id,
            "state": final.get("state"),
            "confidence": final.get("confidence"),
            "verdict_action": getattr(verdict, "action", "pass"),
            "verdict_reason": getattr(verdict, "reason", ""),
            "verdict_notes": getattr(verdict, "notes", []),
            "tools": result.tools_used,
            "denied_actions": denied_actions,
            "reconciled_actions": reconciled_actions,
            "evidence": result.evidence,
            "latency_ms": result.latency_ms,
            "usage_total": result.usage,
            "escalation_reason": (final.get("escalation") or {}).get("reason", ""),
            "identity_verified": state.issue.verified_identity,
            "issue_card": state.issue.to_prompt(),
        }
        # 对整条记录脱敏，包括 issue_card（症状/描述中可能夹带临时密码等凭据，S02 r1/r3）
        self._append(_mask(record))

    def log_tool(self, session_id: str, name: str, args: Dict[str, Any], status: str, ms: int) -> None:
        self._append(
            {
                "ts": _now(),
                "session_id": session_id,
                "type": "tool",
                "tool": name,
                "args": _mask(args),
                "status": status,
                "ms": ms,
            }
        )

    def _append(self, record: Dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
