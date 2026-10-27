"""Project-venv checks, including actual HTTP without adding dependencies."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

OUT = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("pinned_driver", OUT / "execution_driver.py")
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
d.OUT = OUT / "pinned"
d.OUT.mkdir(exist_ok=True)
for name in ("tmp", "evidence", "fixtures", "traces"):
    (d.OUT / name).mkdir(exist_ok=True)
os.environ["TEMP"] = os.environ["TMP"] = str(d.OUT / "tmp")
d.prepare_fixtures()
env = dict(os.environ)
env.update(LLM_PROVIDER="mock", TRACE_LOG_PATH=str(d.OUT / "traces/startup.jsonl"),
           EVAL_TRACE_PATH=str(d.OUT / "traces/regression.jsonl"),
           EVAL_REPORT_PATH=str(d.OUT / "regression_report.json"))
results = []
for name, args in [("selftest", ["-m", "app.tools.selftest"]), ("regression", ["evals/runner.py", "--provider", "mock"])]:
    r = subprocess.run([sys.executable, "-B", "-X", "utf8"] + args, env=env, cwd=d.ROOT,
                       capture_output=True, text=True, encoding="utf-8", timeout=90)
    (d.OUT / (name + ".log")).write_text(r.stdout + "\n" + r.stderr, encoding="utf-8")
    results.append({"name": name, "returncode": r.returncode})

# Same controlled component suite, using project-pinned packages.
d.components()

env["TRACE_LOG_PATH"] = str(d.OUT / "traces/http.jsonl")
base = "http://127.0.0.1:18764"
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
def req(path, payload=None):
    raw = json.dumps(payload).encode() if payload is not None else None
    with opener.open(urllib.request.Request(base + path, data=raw, headers={"Content-Type": "application/json"}), timeout=5) as r:
        body = r.read().decode()
        return {"status": r.status, "body": json.loads(body) if path.startswith("/api/") else {"has_chat": "api/chat" in body}}

with (d.OUT / "uvicorn.log").open("w", encoding="utf-8") as log:
    server = subprocess.Popen([sys.executable, "-B", "-X", "utf8", "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "18764"],
                              env=env, cwd=d.ROOT, stdout=log, stderr=log,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        for _ in range(100):
            try:
                health = req("/api/health")
                break
            except Exception:
                if server.poll() is not None:
                    raise RuntimeError("Server exited during startup")
                time.sleep(0.1)
        else:
            raise RuntimeError("HTTP startup timeout")
        chat = req("/api/chat", {"message": "VPN连不上报809"})
        sid = chat["body"]["session_id"]
        results.append({"api": {"index": req("/"), "health": health, "chat": chat,
                                "session": req("/api/session/" + sid),
                                "reset": req("/api/session/" + sid + "/reset", {})}})
    finally:
        server.terminate()
        server.wait(timeout=15)
(OUT / "pinned_runtime_results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(results[:2], ensure_ascii=False))
print("Pinned actual HTTP checks complete")
