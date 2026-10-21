"""search_kb 工具：BM25 检索知识库 markdown 文章。"""
import os
from typing import Any, Dict

from app.retrieval.bm25 import BM25Index, best_snippet
from app.tools.registry import Tool, ToolContext


class KBSearchTool(Tool):
    name = "search_kb"
    description = (
        "搜索 IT 知识库（排障指南、FAQ、政策说明）。"
        "返回最相关文章的标题、路径与摘要片段。诊断前先查这里。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索词，如 'vpn 809'、'密码重置'"},
            "top_k": {"type": "integer", "description": "返回条数，默认 3"},
        },
        "required": ["query"],
    }

    def __init__(self, kb_dir: str):
        self._kb_dir = kb_dir
        self._index: Dict[str, Any] = {}

    def _load(self) -> None:
        docs: Dict[str, str] = {}
        for fname in sorted(os.listdir(self._kb_dir)):
            if fname.endswith(".md"):
                path = os.path.join(self._kb_dir, fname)
                with open(path, "r", encoding="utf-8") as f:
                    docs[fname] = f.read()
        self._index = {"bm25": BM25Index(docs)}

    def run(self, ctx: ToolContext, query: str, top_k: int = 3) -> Dict[str, Any]:
        if not self._index:
            self._load()
        hits = self._index["bm25"].search(query, top_k=top_k)
        results = [
            {
                "title": h.doc_id.replace(".md", "").replace("-", " "),
                "path": f"kb/{h.doc_id}",
                "snippet": best_snippet(h.text, query),
                "score": h.score,
            }
            for h in hits
        ]
        return {"source": "kb", "query": query, "match_count": len(results), "results": results}
