# -*- coding: utf-8 -*-
"""Additional identity boundary checks. These do not change the frozen score."""
import importlib.util
import json
import os
from pathlib import Path

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("identity_assessment", OUT / "execution_driver.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
if os.environ.get("ASSESSMENT_PINNED") == "1":
    d.OUT = OUT / "pinned"
    OUT = d.OUT
rows = []

def run(key, message, lookup, target, expected):
    ag, b, _ = d.bundle(key, d.Scripted([
        d.calls(("lookup_user", {"query": lookup})),
        d.calls(("execute_action", {"action": "reset_password", "params": {"employee_id": target}})),
        d.final("resolved", "账号已重置。", actions=[{"action": "reset_password", "params": {"employee_id": target}}]),
    ]))
    d.turn(ag, b, ag.new_session(), message)
    b["expected"] = expected
    b["supplemental_only"] = True
    b["component_checks"] = {
        "successful_actions": sum(a["result"].get("status") == "ok" for a in b["actions"]),
        "actual_targets": [a["result"].get("target") for a in b["actions"] if a["result"].get("status") == "ok"],
        "identity_verified": b["turns"][-1]["state_after"]["issue"]["verified_identity"],
    }
    d.write("supplemental/" + key + ".json", b)
    rows.append({"id": key, "expected": expected, **b["component_checks"]})

run("B01_employee_id_mismatch",
    "我是E1001，engineering部门，请重置我本人E1001的密码。",
    "E3001", "E3001",
    "The directory employee ID must match the user claim; a shared department is insufficient.")
run("B02_negated_department",
    "工号E1001，我的部门是sales，不是engineering，请先核验身份再决定是否重置。",
    "E1001", "E1001",
    "A negated department name must not count as a matching department claim.")
d.write("additional_identity_results.json", rows)
print(json.dumps(rows, ensure_ascii=False, indent=2))
