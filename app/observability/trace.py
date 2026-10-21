"""TraceLogger：JSONL 结构化运行日志（可观测性）。

每轮追加一条记录：轮次、结论、工具调用链、token 用量、延迟。
演示与 README 用它支撑"成本与延迟"维度的实测数据。
"""
import json
import os
from typing import Any, Dict, Optional


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
        record: Dict[str, Any] = {
            "ts": _now(),
            "session_id": state.session_id,
            "turn": state.turn_count,
            "state": final.get("state"),
            "confidence": final.get("confidence"),
            "verdict_action": getattr(verdict, "action", "pass"),
            "verdict_reason": getattr(verdict, "reason", ""),
            "verdict_notes": getattr(verdict, "notes", []),
            "tools": result.tools_used,
            "evidence": result.evidence,
            "latency_ms": result.latency_ms,
            "usage_total": result.usage,
            "escalation_reason": (final.get("escalation") or {}).get("reason", ""),
            "issue_card": state.issue.to_prompt(),
        }
        self._append(record)

    def log_tool(self, session_id: str, name: str, args: Dict[str, Any], status: str, ms: int) -> None:
        self._append(
            {
                "ts": _now(),
                "session_id": session_id,
                "type": "tool",
                "tool": name,
                "args": args,
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
