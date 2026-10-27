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

# Step 7：按键名脱敏的敏感字段（覆盖嵌套对象和列表）
_SENSITIVE_KEYS = {
    "temporary_password", "password", "passwd", "pwd", "token", "secret",
    "api_key", "apikey", "credential", "credentials", "access_token",
    "refresh_token", "private_key", "session_key", "auth_token",
}

# 本轮收集到的已知秘密值（来自工具回执），用于在自由文本中遮盖
_known_secrets: list = []


def _register_secret(value: str) -> None:
    """注册一个已知秘密值，后续 _mask 会在自由文本中遮盖它。"""
    if isinstance(value, str) and value and len(value) >= 3:
        if value not in _known_secrets:
            _known_secrets.append(value)


def _mask(value: Any) -> Any:
    """递归脱敏：按键名 + 正则 + 已知秘密值三重遮盖。"""
    if isinstance(value, str):
        masked = value
        for pat in _CREDENTIAL_PATTERNS:
            masked = pat.sub(lambda m: m.group(1) + "***" if m.groups() else "***", masked)
        # 已知秘密值若出现在自由文本中，也要遮盖
        for secret in _known_secrets:
            if secret in masked:
                masked = masked.replace(secret, "***")
        return masked
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key_lower = str(k).lower()
            if key_lower in _SENSITIVE_KEYS:
                out[k] = "***"
                # 同时把该值注册为已知秘密，供自由文本遮盖
                if isinstance(v, str):
                    _register_secret(v)
            else:
                out[k] = _mask(v)
        return out
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
        decision=None,
    ) -> None:
        # Step 7：先收集本轮工具回执中的秘密值，供自由文本遮盖使用
        _known_secrets.clear()
        for r in state.tool_records:
            if r.turn != state.turn_count or not isinstance(r.data, dict):
                continue
            inner = r.data.get("result") if isinstance(r.data.get("result"), dict) else {}
            for sk in _SENSITIVE_KEYS:
                val = inner.get(sk)
                if isinstance(val, str):
                    _register_secret(val)

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

        # 对外状态优先取统一决议（FinalDecision.state），保证 trace 与 API/会话一致。
        decision_state = getattr(decision, "state", None) if decision is not None else None
        record: Dict[str, Any] = {
            "ts": _now(),
            "session_id": state.session_id,
            "turn": state.turn_count,
            "issue_id": getattr(state.issue, "issue_id", None) or state.session_id,
            "state": decision_state if decision_state is not None else final.get("state"),
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
            # 契约校验修复记录：字段路径与修复方式，便于审计 normalize_final 的降级操作
            "validation_notes": final.get("_validation_notes") or [],
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

    def log_execution(
        self,
        session_id: str,
        issue_id: Optional[str],
        turn: int,
        execution_id: str,
        action: str,
        target: str,
        resource: str,
        business_status: str,
        result_reference: str,
        source: str = "native",
    ) -> None:
        """统一的动作执行日志入口（原生 / 身份补偿重试 / 对账补执行共用）。

        每条记录关联：会话、问题、轮次、执行标识(call_id)、动作、目标、资源、
        业务结果(ok/denied/error)、结果引用、来源路径。便于审计 T05.2 补执行
        与 S05.1 补充输入是否可追溯。
        """
        self._append(
            _mask(
                {
                    "ts": _now(),
                    "type": "execution",
                    "session_id": session_id,
                    "issue_id": issue_id,
                    "turn": turn,
                    "execution_id": execution_id,
                    "source": source,  # native | identity_retry | reconcile
                    "action": action,
                    "target": target,
                    "resource": resource,
                    "business_status": business_status,
                    "result_reference": result_reference,
                }
            )
        )

    def _append(self, record: Dict[str, Any]) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")
