# IT Helpdesk Agent

A take-home prototype for conversational IT support: investigate an employee's problem with tools, retain context across turns, perform permitted **simulated** actions, and prepare a contextual handoff when human help is needed.

Start here: [three short demos](docs/DEMO.md) · [submission test report](TEST_REPORT.md) · [test cases](evals/cases.yaml).

The strongest supported scenarios are password/account issues, VPN troubleshooting and access requests. Software performance and multi-system incidents support investigation and handoff. This prototype does not administer real Okta, Salesforce, Jenkins, Snowflake or ticketing systems; employee records, incidents and action results are local demonstration fixtures.

## Install and run

Use **Python 3.11** (submission tested with 3.11.17), Git and a browser. Run commands from the repository root. Mock mode needs no API key or network after dependencies are installed. Real conversation demos require a model API and incur usage charges.

### Windows PowerShell

```powershell
git clone https://github.com/GiftedPopcorn/IT-Helpdesk-Agent.git
cd IT-Helpdesk-Agent
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
$env:LLM_PROVIDER = 'mock'
.\.venv\Scripts\python.exe -B main.py
```

Using the virtual environment's Python directly avoids activation-policy issues. If `.env` already exists, keep it instead of copying over it. Dependencies are version-pinned in `requirements.txt`.

### macOS / Linux

```bash
git clone https://github.com/GiftedPopcorn/IT-Helpdesk-Agent.git
cd IT-Helpdesk-Agent
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
LLM_PROVIDER=mock .venv/bin/python -B main.py
```

The POSIX commands are provided for convenience; the submission was tested on Windows, not independently on macOS/Linux.

Open **http://localhost:8000**. Health check: **http://localhost:8000/api/health**; API documentation: **http://localhost:8000/docs**. Health shows the active provider; `status: ok` confirms service startup, not API-key validity. Stop with `Ctrl+C`. Sessions are in memory and disappear on restart.

If port 8000 is occupied, use the same environment to run `python -m uvicorn main:app --host 127.0.0.1 --port 8001`. `main.py` binds to `0.0.0.0`; the demo has no API authentication, so run in a local/trusted environment.

## Configure a real model

Edit your local `.env` (never commit it):

```dotenv
LLM_PROVIDER=deepseek
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.deepseek.com
LLM_MODEL=deepseek-chat
LLM_TEMPERATURE=0.2
```

If you set the PowerShell provider override above, remove it before restarting:

```powershell
Remove-Item Env:LLM_PROVIDER -ErrorAction SilentlyContinue
.\.venv\Scripts\python.exe -B smoke.py
.\.venv\Scripts\python.exe -B main.py
```

Environment variables override `.env`; restart after configuration changes. `smoke.py` calls the selected provider. On macOS/Linux, restart without the `LLM_PROVIDER=mock` prefix. An OpenAI-compatible adapter is also included; see `.env.example`. Submitted real-model evidence uses DeepSeek only.

**Mock is scripted**, intended for offline smoke tests and deterministic regression, not proof of natural-language reasoning. Use DeepSeek for free-form demos. For connection failures, check provider, key balance, base URL and local proxy/network settings; distinguish infrastructure failures from semantic test failures.

## Run tests

Windows, from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe -B -m app.tools.selftest
.\.venv\Scripts\python.exe -B evals/runner.py --provider mock
# Requires real API configuration; incurs usage charges:
.\.venv\Scripts\python.exe -B evals/runner.py --provider deepseek
```

On macOS/Linux substitute `.venv/bin/python`. The runner executes all **15** cases from `evals/cases.yaml` through the agent loop. Exit code **0** means all assertions and the credential-leak check passed; **1** means at least one failed. Retain failures when comparing versions.

| Output | Meaning |
|---|---|
| `evals/report.mock.json` | Scripted Mock results |
| `evals/report.real.json` | Real-model results, including per-case assertions and replies |
| `logs/trace_eval.jsonl` | Evaluation trace, overwritten by the next runner invocation |
| `logs/trace.jsonl` | Application trace, ignored by Git |

For separate runs, first create an output directory, then set `EVAL_REPORT_PATH` and `EVAL_TRACE_PATH` to distinct writable paths. Reports can contain synthetic temporary credentials in responses; do not publish real support conversations or employee secrets.

**Submission results (2026-10-27): Mock 15/15; DeepSeek 12/15.** The real run failed `vpn-chg-001`, `fuzzy-008` and `mixed-013`; see [TEST_REPORT.md](TEST_REPORT.md) for the assertions and interpretation. A preceding sandbox network failure is retained separately. The earlier 120-group expanded assessment, controlled boundary probes and browser checks are separate evidence, not a substitute for these 15 cases. Historical “13/13” is not the current result.

Verify runtime/data/test hashes without API calls:

```powershell
.\.venv\Scripts\python.exe -B evals/verify_submission.py
```

## Three demonstrations

Start a new conversation between demos. [docs/DEMO.md](docs/DEMO.md) provides exact inputs and observations:

1. **Normal resolution:** forgot password → provide employee ID and department → simulated reset.
2. **Permission boundary:** company-wide admin request → refuse and prepare a human handoff.
3. **Unresolved investigation:** VPN drops → clarification → insufficient information → request human help with the existing context.

These are live expected behaviors, not prerecorded responses. The third flow explicitly discloses that automatic second-turn escalation failed in the submission run; user-requested handoff is shown separately. “Escalated” means a summary was prepared, not that a ServiceNow/Jira ticket was sent.

## Architecture and trade-offs

```text
Browser → FastAPI → AgentLoop ↔ LLM (native tool calling)
                       ├─ ToolRegistry → KB / status / directory / tickets / policy / actions
                       ├─ SessionState → recent messages + structured issue facts
                       ├─ OutcomeEngine → policy, progress and final-state checks
                       └─ TraceLogger → JSONL events
```

| Component | Responsibility |
|---|---|
| `app/agent/loop.py` | Bounded investigation, tool calls, fact merging and reply reconciliation |
| `app/agent/state.py` | Identity claims, issue facts, hypotheses, action records and multi-issue state |
| `app/agent/outcome.py` | Resolution/escalation review and handoff summaries |
| `app/tools/` | Six tools, argument checks, timeouts, policy and simulated actions |
| `app/llm/` | Scripted Mock and OpenAI-compatible model adapter |
| `app/static/index.html` | Chat, status, tool activity and handoff display |
| `data/` | Seven KB articles, status/changes, directory, prior tickets and policy |
| `evals/` | Case definitions, runner, submitted reports and evidence |

An agent is useful because answers can require multiple sources and turns: error 809 connects to change CHG-339, troubleshooting comes from the KB, and closure needs user feedback. Retrieval alone cannot perform actions or manage that workflow.

- **Hand-written loop:** explicit control over execution, parsing, reconciliation and stopping; persistence and concurrency protection remain future work.
- **BM25:** a small local KB needs no embedding service. This is a prototype trade-off, not a large-corpus benchmark.
- **Code-enforced policy:** execution checks identity, role, target and resources. Known identity-parsing failures below mean these checks are not a complete security guarantee.
- **Two memory representations:** recent messages plus structured facts preserve context outside the message window. Handoff completeness is evaluated, not assumed.
- **Four response states:** `need_info`, `pending_user`, `resolved`, `escalated`. The first two are nonterminal; resolving one issue must not close other open issues.

## Known limitations and next work

1. Quoted UI text, English forwarded text and a colleague's first-person quotation can still be mistaken for the employee's department and permit a simulated reset. Improve source/negation attribution; use authenticated identity in production.
2. Reply post-processing can leave broken sentences, repeat an old temporary password, or claim success without a matching current action. Bind statements to current receipts and generate complete sentences from final state.
3. Recovery acknowledgements and automatic escalation are not reliable across all phrasings. Quoted/negated handoff requests, inconsistent reason fields and post-handoff audit gaps remain.
4. Mock expectations can conflict with intended multi-issue behavior; passing scripted tests is not a substitute for real-model and adversarial checks.

Further production work: SSO-bound sessions, persistent storage, per-session concurrency control, real action/ticket integrations, broader regression coverage and latency/cost budgets. Confidence values are model estimates, not calibrated probabilities. Replies and fixture data are primarily Chinese; this README is English for reviewers.
