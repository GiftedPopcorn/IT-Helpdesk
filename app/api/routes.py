"""HTTP 路由：薄层，只做 HTTP <-> 内部对象转换（呼应 MindBridge harness 思路）。"""
import os
from typing import Any, Dict, Optional

from fastapi import FastAPI
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.agent.loop import AgentLoop

_STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None


def register_routes(app: FastAPI, agent: AgentLoop, settings) -> None:
    @app.post("/api/chat")
    def chat(req: ChatRequest) -> Dict[str, Any]:
        session_id = req.session_id or agent.new_session()
        result = agent.run_turn(session_id, req.message)
        payload = result.to_dict()
        payload["session_id"] = session_id
        return payload

    @app.get("/api/session/{session_id}")
    def get_session(session_id: str) -> Dict[str, Any]:
        state = agent.store.get_or_create(session_id)
        return state.to_dict()

    @app.post("/api/session/{session_id}/reset")
    def reset_session(session_id: str) -> Dict[str, Any]:
        ok = agent.store.reset(session_id)
        return {"reset": ok}

    @app.get("/api/health")
    def health() -> Dict[str, Any]:
        return {
            "status": "ok",
            "provider": settings.llm_provider,
            "model": settings.llm_model,
        }

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(os.path.join(_STATIC_DIR, "index.html"))
