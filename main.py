"""项目入口：python main.py 即可启动（默认 http://localhost:8000）。"""
import uvicorn

from app.agent import build_agent
from app.api.routes import register_routes
from app.config import get_settings
from app.llm import build_llm
from app.observability.trace import TraceLogger


def create_app():
    from fastapi import FastAPI

    settings = get_settings()
    llm = build_llm(settings)
    tracer = TraceLogger(settings.trace_log_path)
    agent = build_agent(settings, llm, tracer)

    app = FastAPI(title="IT Helpdesk Agent", version="1.0.0")
    register_routes(app, agent, settings)
    return app


app = create_app()

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
