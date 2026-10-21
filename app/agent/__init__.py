"""Agent 层：组装 AgentLoop 的统一入口。"""
from app.agent.loop import AgentLoop, TurnResult, parse_final_json
from app.agent.outcome import OutcomeEngine
from app.agent.store import SessionStore
from app.agent.state import SessionState


def build_agent(settings, llm, tracer=None) -> AgentLoop:
    """按配置装配完整 Agent（LLM 由外部注入，便于评测切换 provider）。"""
    from app.tools import build_default_registry
    from app.tools.policy import PolicyEngine
    import os

    registry = build_default_registry(settings)
    policy = PolicyEngine(os.path.join(settings.data_dir, "policy.json"))
    outcome = OutcomeEngine(policy, confidence_floor=settings.agent_confidence_floor)
    store = SessionStore()
    return AgentLoop(
        llm=llm, registry=registry, store=store, outcome=outcome,
        policy=policy, settings=settings, tracer=tracer,
    )


__all__ = ["AgentLoop", "TurnResult", "OutcomeEngine", "SessionStore", "SessionState", "build_agent", "parse_final_json"]
