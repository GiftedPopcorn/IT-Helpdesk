"""评测 runner：离线跑全部用例 → 逐项断言 → 指标报告。

用法：
    python evals/runner.py --provider mock        # 离线免费、结果可复现（CI 用）
    python evals/runner.py --provider deepseek    # 真实 LLM 指标（需 .env 配置 key）

设计要点：
- 直接调 AgentLoop（不开 HTTP）：评测走的就是生产同一条代码路径
- 结果写 evals/report.json（全量对话轨迹 + 断言明细），控制台输出摘要表格
- exit code：全部通过为 0，有用例失败为 1（可直接接 CI）
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # 固定工作目录，保证 data/ logs/ 相对路径与 API 运行时一致

import yaml

from app.agent import build_agent
from app.config import get_settings
from app.llm import build_llm
from app.observability.trace import TraceLogger


def load_cases(path: str):
    with open(path, "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("cases", [])


def run_case(agent, case):
    """跑一个用例：新会话 + 按轮发送；可选注入工具故障（registry.chaos）。"""
    sid = agent.new_session()
    chaos = case.get("inject_tool_failure") or None
    if chaos:
        agent.registry.chaos.add(chaos)
    turns = []
    try:
        for msg in case.get("turns", []):
            turns.append(agent.run_turn(sid, str(msg)))
    finally:
        if chaos:
            agent.registry.chaos.discard(chaos)
    return sid, turns


def check_case(agent, case, sid, turns):
    """逐项断言。返回 checks 列表（每项含 name/ok/detail）。"""
    exp = case.get("expected") or {}
    checks = []

    def chk(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    # 1) 每轮状态（诊断路径是否符合预期节奏）
    for i, want in enumerate(exp.get("turn_states") or []):
        got = turns[i].state if i < len(turns) else "missing"
        chk(f"第{i + 1}轮状态={want}", got == want, f"实际={got}")

    # 2) 终态
    final = turns[-1].state if turns else "missing"
    if exp.get("final_state"):
        chk(f"终态={exp['final_state']}", final == exp["final_state"], f"实际={final}")

    # 3) 进入终态前必须追问过（防"上来就给结论"）
    if exp.get("must_clarify_first"):
        clarified = len(turns) > 1 and any(t.state == "need_info" for t in turns[:-1])
        chk("终态前出现过追问轮", clarified, f"各轮状态={[t.state for t in turns]}")

    # 4) 工具覆盖
    used = {t["name"] for tr in turns for t in tr.tools_used}
    for name in exp.get("must_use_tools") or []:
        chk(f"调用过 {name}", name in used, f"实际={sorted(used) or '无'}")
    for name in exp.get("must_not_use_tools") or []:
        chk(f"未调用 {name}", name not in used, f"实际={sorted(used)}")

    # 5) 回复内容（任一轮；防编造用 must_not_include）
    all_reply = "\n".join(t.reply for t in turns)
    for kw in exp.get("reply_must_include") or []:
        chk(f"回复包含「{kw}」", kw in all_reply)
    for kw in exp.get("reply_must_not_include") or []:
        chk(f"回复不包含「{kw}」", kw not in all_reply)

    # 6) 动作对账：以会话状态里 execute_action 的真实落账为准，不信 LLM 嘴上说
    st = agent.store.get_or_create(sid)
    executed = {
        (r.args or {}).get("action")
        for r in st.tool_records
        if r.name == "execute_action" and r.status == "ok"
    }
    for a in exp.get("action_executed") or []:
        chk(f"真实执行过 {a}", a in executed, f"实际执行={sorted(x for x in executed if x) or '无'}")
    for a in exp.get("must_not_execute") or []:
        chk(f"未执行 {a}", a not in executed, f"实际执行={sorted(x for x in executed if x) or '无'}")

    # 7) 升级校验：原因 + 摘要必须带关键上下文（带完整信息交接）
    esc = [t for t in turns if t.state == "escalated" and t.escalation]
    if exp.get("escalation_reason"):
        got = esc[-1].escalation.get("reason") if esc else "（未升级）"
        chk(f"升级原因={exp['escalation_reason']}", bool(esc) and got == exp["escalation_reason"], f"实际={got}")
    for kw in exp.get("escalation_must_include") or []:
        hit = any(kw in (t.escalation or {}).get("summary", "") for t in esc)
        chk(f"升级摘要包含「{kw}」", hit)

    # 8) 置信度下限：有强信号佐证的终态，置信度不应低于阈值（声明才检查）
    if exp.get("min_confidence") and turns:
        got = turns[-1].confidence
        chk(
            f"终态置信度≥{exp['min_confidence']}", got >= exp["min_confidence"],
            f"实际={got}",
        )
    return checks


def main():
    ap = argparse.ArgumentParser(description="IT Helpdesk Agent 评测")
    ap.add_argument("--provider", default=None, choices=["mock", "deepseek", "openai"],
                    help="覆盖 .env 里的 LLM_PROVIDER")
    ap.add_argument("--cases", default=str(ROOT / "evals" / "cases.yaml"))
    args = ap.parse_args()

    settings = get_settings()
    updates = {"trace_log_path": "logs/trace_eval.jsonl"}  # 评测 trace 与线上分开
    if args.provider:
        updates["llm_provider"] = args.provider
    settings = settings.model_copy(update=updates)

    if settings.llm_provider != "mock" and not settings.llm_api_key:
        sys.exit(f"provider={settings.llm_provider} 需要 API Key：请复制 .env.example 为 .env 并配置 LLM_API_KEY")

    llm = build_llm(settings)
    agent = build_agent(settings, llm, tracer=TraceLogger(settings.trace_log_path))
    cases = load_cases(args.cases)
    print(f"provider={settings.llm_provider}  model={settings.llm_model}  cases={len(cases)}\n")

    report_cases, t_all = [], time.monotonic()
    for case in cases:
        t0 = time.monotonic()
        sid, turns = run_case(agent, case)
        wall_ms = int((time.monotonic() - t0) * 1000)
        checks = check_case(agent, case, sid, turns)
        passed = all(c["ok"] for c in checks)
        exp = case.get("expected") or {}
        report_cases.append({
            "id": case["id"],
            "passed": passed,
            "wall_ms": wall_ms,
            "expected_final": exp.get("final_state"),
            "got_final": turns[-1].state if turns else "missing",
            "checks": checks,
            "turns": [
                {
                    "state": t.state,
                    "confidence": t.confidence,
                    "reply": t.reply,
                    "tools": [tu["name"] for tu in t.tools_used],
                    "escalation_reason": (t.escalation or {}).get("reason") if t.escalation else None,
                    "latency_ms": t.latency_ms,
                }
                for t in turns
            ],
            "tokens": turns[-1].usage if turns else {},
        })
        mark = "PASS" if passed else "FAIL"
        print(f"[{mark}] {case['id']:<22} 终态={turns[-1].state if turns else '-':<12} {wall_ms}ms")
        if not passed:
            for c in checks:
                if not c["ok"]:
                    print(f"        x {c['name']}  {c['detail']}")

    # ---- 指标聚合（对应作业"解决 vs 升级边界判断"的量化度量）----
    n = len(report_cases)
    passed_n = sum(1 for c in report_cases if c["passed"])
    turn_n = sum(len(c["turns"]) for c in report_cases) or 1
    resolved_exp = [c for c in report_cases if c["expected_final"] == "resolved"]
    escalated_exp = [c for c in report_cases if c["expected_final"] == "escalated"]
    metrics = {
        "cases_total": n,
        "cases_passed": passed_n,
        "pass_rate": round(passed_n / n, 3) if n else 0.0,
        "resolution_rate": round(
            sum(1 for c in resolved_exp if c["got_final"] == "resolved") / len(resolved_exp), 3
        ) if resolved_exp else None,
        "escalation_recall": round(
            sum(1 for c in escalated_exp if c["got_final"] == "escalated") / len(escalated_exp), 3
        ) if escalated_exp else None,
        "misescalation_count": sum(
            1 for c in report_cases
            if c["expected_final"] not in ("escalated", None) and c["got_final"] == "escalated"
        ),
        "avg_turn_latency_ms": round(
            sum(t["latency_ms"] for c in report_cases for t in c["turns"]) / turn_n
        ),
        "total_tokens": sum(
            c["tokens"].get("prompt", 0) + c["tokens"].get("completion", 0) for c in report_cases
        ),
        "wall_ms_total": int((time.monotonic() - t_all) * 1000),
    }

    print("\n===== 指标汇总 =====")
    for k, v in metrics.items():
        print(f"{k:<24} {v}")

    out = ROOT / "evals" / "report.json"
    out.write_text(
        json.dumps(
            {"provider": settings.llm_provider, "metrics": metrics, "cases": report_cases},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\n报告已写入 {out}")
    sys.exit(0 if passed_n == n else 1)


if __name__ == "__main__":
    main()
