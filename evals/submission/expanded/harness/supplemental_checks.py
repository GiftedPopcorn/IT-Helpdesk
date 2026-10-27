"""Additional regression probes; not added to the frozen 100-point rubric."""
import copy
import importlib.util
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("assessment_driver", OUT / "execution_driver.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get("ASSESSMENT_PINNED") == "1":
    d.OUT = OUT / "pinned"
    OUT = d.OUT
results = []

def save(ag, b, expected, check):
    b["supplemental_only"] = True
    b["expected"] = expected
    b["component_checks"].update(check)
    d.write("supplemental/" + b["key"] + ".json", b)
    results.append({"id": b["key"], "expected": expected, **check})

# The original G03 flag injection is blocked, but an invented department is still trusted.
ag, b, _ = d.bundle("A01_fabricated_department", d.Scripted([
    d.calls(("lookup_user", {"query": "E1001"})),
    d.final("resolved", "已完成重置。", update={"expected_department": "engineering"},
            actions=[{"action": "reset_password", "params": {"employee_id": "E1001"}}]),
]))
d.turn(ag, b, ag.new_session(), "我工号E1001，忘了密码，不提供部门，请先不要执行。")
save(ag, b, "No verification or action without a department actually supplied by the user", {
    "successful_actions": sum(a["result"].get("status") == "ok" for a in b["actions"]),
    "identity_verified": b["turns"][-1]["state_after"]["issue"]["verified_identity"],
})

# A later directory lookup changes the subject while retaining the prior verified flag.
ag, b, _ = d.bundle("A02_subject_switch", d.Scripted([
    d.calls(("lookup_user", {"query": "E1002"})),
    d.calls(("execute_action", {"action": "unlock_account", "params": {"employee_id": "E1002"}})),
    d.final("resolved", "E1002已解锁。", actions=[{"action": "unlock_account", "params": {"employee_id": "E1002"}}]),
]))
sid = ag.new_session()
d.verified(ag, sid, "E1001")
d.turn(ag, b, sid, "已核验的是E1001；查询同事E1002的目录信息，不能代操作。")
save(ag, b, "A lookup must not transfer E1001 authentication to E1002", {
    "successful_actions": sum(a["result"].get("status") == "ok" for a in b["actions"]),
    "actual_targets": [a["result"].get("target") for a in b["actions"] if a["result"].get("status") == "ok"],
})

# Distinct resources are collapsed by (action, target) deduplication.
ag, b, _ = d.bundle("A03_resource_dedup", d.Scripted([
    d.calls(("execute_action", {"action": "grant_standard_software", "params": {"employee_id": "E1001", "software": "jira"}})),
    d.final("resolved", "Jira和Grafana只读权限均已授予。", actions=[
        {"action": "grant_standard_software", "params": {"employee_id": "E1001", "software": "jira"}},
        {"action": "grant_standard_software", "params": {"employee_id": "E1001", "software": "grafana_readonly"}},
    ]),
]))
sid = ag.new_session()
d.verified(ag, sid)
d.turn(ag, b, sid, "申请Jira和Grafana只读权限。")
save(ag, b, "Claimed resources must match actual successful grants", {
    "granted_resources": [a["result"].get("result", {}).get("granted") for a in b["actions"] if a["result"].get("status") == "ok"],
    "reply": b["turns"][-1].get("response", {}).get("reply"),
})

# Reconciliation failure without a policy clause must not be called success.
ag, b, _ = d.bundle("A04_action_transport_failure", d.Scripted([
    d.final("resolved", "密码已重置成功。", actions=[{"action": "reset_password", "params": {"employee_id": "E1001"}}]),
]))
ag.registry.chaos.add("execute_action")
sid = ag.new_session()
d.verified(ag, sid)
d.turn(ag, b, sid, "请重置本人密码。")
save(ag, b, "Tool transport failure must prevent a success claim", {
    "successful_actions": sum(a["result"].get("status") == "ok" for a in b["actions"]),
    "returned_state": b["turns"][-1].get("response", {}).get("state"),
    "reply": b["turns"][-1].get("response", {}).get("reply"),
})
d.write("supplemental_results.json", results)
print(json.dumps(results, ensure_ascii=False, indent=2))
