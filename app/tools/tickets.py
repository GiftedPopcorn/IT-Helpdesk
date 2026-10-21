"""search_tickets 工具：检索历史已解决工单，寻找相似问题先例。"""
import json
import os
from typing import Any, Dict

from app.retrieval.bm25 import BM25Index
from app.tools.registry import Tool, ToolContext


class SearchTicketsTool(Tool):
    name = "search_tickets"
    description = (
        "检索历史已解决工单。用于寻找相似问题的先例："
        "历史上 agent 解决了什么、什么情况最终转了人工（可参考边界）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词，如 'vpn 809'、'mfa 过期'"},
            "top_k": {"type": "integer", "description": "返回条数，默认 3"},
        },
        "required": ["query"],
    }

    def __init__(self, tickets_path: str):
        self._path = tickets_path
        self._index = None

    def _load(self) -> BM25Index:
        if self._index is None:
            with open(self._path, "r", encoding="utf-8") as f:
                tickets = json.load(f).get("tickets", [])
            docs = {
                t["id"]: " ".join(
                    [t.get("issue_summary", ""), t.get("resolution", ""), " ".join(t.get("tags", []))]
                )
                for t in tickets
            }
            self._index = (BM25Index(docs), {t["id"]: t for t in tickets})
        return self._index

    def run(self, ctx: ToolContext, query: str, top_k: int = 3) -> Dict[str, Any]:
        bm25, tickets_by_id = self._load()
        hits = bm25.search(query, top_k=top_k)
        results = []
        for h in hits:
            t = tickets_by_id[h.doc_id]
            results.append(
                {
                    "id": t["id"],
                    "category": t.get("category"),
                    "issue_summary": t.get("issue_summary"),
                    "resolution": t.get("resolution"),
                    "resolved_by": t.get("resolved_by"),
                }
            )
        return {"source": "ticket_history", "query": query, "match_count": len(results), "results": results}
