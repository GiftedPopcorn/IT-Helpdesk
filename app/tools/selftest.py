"""P1 自测：逐一调用 6 个工具，验证数据源与检索正确性。

用法：python -m app.tools.selftest
"""
import json
import sys

from app.config import get_settings
from app.tools import ToolContext, build_default_registry


def main() -> int:
    settings = get_settings()
    registry = build_default_registry(settings)
    ctx = ToolContext(settings=settings, state=None)
    failures = []

    def run(name, **args):
        result = registry.get(name).run(ctx, **args)
        print(f"\n=== {name} {args} ===")
        print(json.dumps(result, ensure_ascii=False, indent=1)[:900])
        return result

    # 1. KB 检索："vpn 809" 必须命中 vpn-disconnect-windows.md
    r = run("search_kb", query="vpn 809 错误")
    paths = [x["path"] for x in r["results"]]
    if "kb/vpn-disconnect-windows.md" not in paths:
        failures.append(f"BM25 未命中 vpn-disconnect-windows.md，实际: {paths}")
    # 2. KB 检索中文
    run("search_kb", query="密码重置 身份核验")
    # 3. 系统状态
    r = run("get_system_status", service="vpn-gateway")
    if not any(c["id"] == "CHG-339" for c in r["recent_changes"]):
        failures.append("vpn-gateway 变更中缺少 CHG-339")
    # 4. 用户查询：精确工号
    r = run("lookup_user", query="E2001")
    if r["users"][0]["role"] != "executive":
        failures.append("E2001 角色应为 executive")
    # 5. 用户查询：同名歧义
    r = run("lookup_user", query="张伟")
    if r["match_count"] < 2:
        failures.append("张伟 应命中 2 条（同名测试）")
    # 6. 历史工单
    r = run("search_tickets", query="mfa token 过期")
    if not any("T-0203" == x["id"] for x in r["results"]):
        failures.append("工单检索未命中 T-0203")
    # 7. 策略查询
    r = run("check_policy", topic="mfa_reset")
    if r["match_count"] != 1 or r["actions"][0]["agent_allowed"]:
        failures.append("mfa_reset 应为禁止 agent 执行")
    # 8. 动作执行：未核验身份 → 必须 denied
    r = run("execute_action", action="reset_password", params={"employee_id": "E1001"})
    if r["status"] != "denied":
        failures.append("未核验身份时 reset_password 应被 denied")

    # 9. 提示词守卫：关键护栏规则必须存在于拼装后的系统提示词
    #    （防止后续迭代提示词时误删评测所依赖的行为约束）
    from app.agent.state import SessionState
    from app.agent.prompts import build_system_prompt
    policy_summary = registry.get("check_policy").run(ctx)  # 无 topic → 全量策略摘要
    system_prompt = build_system_prompt(SessionState(session_id="selftest"), policy_summary)
    guards = {
        "置信度打分标准": "置信度打分标准",
        "追问轮置信度指引": "不要给 0.5 以下",
        "可代执行动作直接执行": "可代执行的动作必须直接执行",
        "pending_user 边界": "pending_user 只用于用户必须亲自操作",
        "只读工具不进动作账": "只读调查工具",
        "工具故障升级规则": "tool_failure",
        "升级话术含人工": "「人工」",
        "越界即升级": "超出能力",
    }
    for label, needle in guards.items():
        if needle not in system_prompt:
            failures.append(f"提示词缺少关键规则[{label}]：未找到「{needle}」")

    print("\n" + ("=" * 50))
    if failures:
        print("自测失败：")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("全部工具自测通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
