# AgentShield-X: AI Workspace

The sandboxed task environment for **AgentShield-X** (SRS Objective 1, PRD `AI_Workspace_PRD.md`).
A tool-using AI agent runs legitimate tasks and controlled attacks here. Every input, tool call,
memory write and output is logged as structured telemetry. The security layer plugs in later
through four extension points, without touching agent code.

> This component contains **no security logic**. See [`docs/INTEGRATION.md`](docs/INTEGRATION.md)
> for how the guards / policy engine attach.

## Quick start (local, no Docker)

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python -m aiworkspace serve       # open http://localhost:8000
```

You need at least one real model (the console marks unreachable ones as unavailable):

* **Local (free, SRS Ch. 14):** install [Ollama](https://ollama.com), then `ollama pull qwen2.5:7b`
  (good at tool calls) or `ollama pull llama3.1:8b`. The default model is `ollama-qwen2.5`.
  For vLLM / llama.cpp set `VLLM_API_BASE` / `LLAMACPP_API_BASE`.
* **Cloud (optional, budget-capped):** copy `.env.example` to `.env` and set `GEMINI_API_KEY`,
  `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

## Quick start (Docker, PRD NFR-7)

```bash
docker compose up --build                     # http://localhost:8000
docker compose --profile ollama up --build    # + a local Ollama container
```

| Service | Role | Network |
|---|---|---|
| `workspace` | agent loop, LiteLLM, API + UI, telemetry | internal + egress (to reach model APIs) |
| `sandbox` | all tools, RAG, MCP servers, synthetic data | **internal only (no internet)**; read-only image, non-root, all capabilities dropped, ephemeral tmpfs reset on every run |
| `ollama` (optional) | local models | egress |

## Command line

```bash
python -m aiworkspace tasks                                        # list tasks
python -m aiworkspace run --task summarise_report --model ollama-qwen2.5 -v
python -m aiworkspace run --prompt "What is 17% of 2,340? Use the calculator." --model gemini-flash -v
python -m aiworkspace batch --label attack --model ollama-qwen2.5 --repeats 5     # FR-27
python -m aiworkspace batch --label attack --plugin examples.example_guard:register
python -m aiworkspace export --fmt csv --table events --out events.csv            # FR-23
pytest -q                                                                         # acceptance tests
set AIWORKSPACE_LIVE_MODELS=gemini-flash,ollama-qwen2.5 && pytest -q tests/test_live_model.py -s   # real-model tests
```

## What is where

```
aiworkspace/
  agent.py        agent loop: task → model → tool call → observe → … → final answer (FR-6)
  models.py       ModelClient: every model call goes through LiteLLM (FR-1..5)
  gateway.py      execute_tool(name, args, context): the single tool gateway (FR-9)
  memory.py       long-term memory; write_memory() is the only write path (FR-14, FR-16)
  hooks.py        the four pass-through extension points (FR-17..19)
  telemetry.py    SQLite logger in the SRS Ch. 9.1 schema, live streaming, CSV/JSON export (FR-21..23)
  tasks.py        task files + success / attack-success evaluation (FR-24..26)
  workspace.py    orchestration: reset → seed → run → evaluate → log; batches & repeats
  api.py, ui/     FastAPI backend + task console (FR-28..30)
  sandbox/        tools with FR-10 metadata, FAISS RAG, MCP bridge, reset, sandbox HTTP server
config/           workspace.yaml (agent, hooks, provenance, sandbox), models.yaml, mcp_servers.yaml
sandbox_seed/     synthetic data: report.pdf, protected_data.txt, fake keys, contacts, mock DB, KB, inbox, web
tasks/            9 benign tasks + 7 attack tasks (one per SRS Ch. 10 category)
mcp_servers/      sample MCP server (calendar)
examples/         example_guard.py: how a guard plugs in (example only)
```

## Models (FR-1..FR-3)

Choose per run in the UI, with `--model`, or with `model:` in a task file. Registered in
`config/models.yaml`: Gemini / OpenAI / Anthropic (API keys), Ollama, vLLM, llama.cpp
(OpenAI-compatible). Any raw LiteLLM model string also works (UI: "Other LiteLLM model string").
Paid usage is capped by `budget.max_usd`. Each model call logs model name, latency, tokens and cost (FR-5).

## Metrics (SRS Ch. 13)

The Summary tab, `GET /api/summary` and the CLI batch output report, per model × defence
configuration × task category: task success rate, attack success rate (ASR), unsafe action
rate, tool misuse rate, recovery rate, false blocking rate, average run time and time spent
inside the security layer (latency overhead). `runs.defence` records the active configuration
(`baseline`, or the enabled extension points and handlers), so Experiment A (baseline) and
Experiments B–F (defended) are directly comparable. Detector precision/recall/F1 and CPU/GPU
overhead are computed by the ML pipeline from the exported telemetry.

## Tests

`tests/test_workspace.py` covers the acceptance criteria. It swaps the model layer for a
test-only scripted client (`tests/scripted_model.py`) so the plumbing is tested
deterministically; that client is never used by the workspace itself.
`tests/test_live_model.py` runs the same tasks on real models when `AIWORKSPACE_LIVE_MODELS` is set.

## Acceptance criteria (PRD section 9)

| # | Criterion | How it is met / tested |
|---|---|---|
| 1 | same task on a cloud and a local model by config only | `--model gemini-flash` vs `--model ollama-qwen2.5`; `tests/test_live_model.py` |
| 2 | legitimate multi-tool task in the sandbox | `meeting_followup`, `calendar_mcp`, … (`test_benign_tasks_succeed`) |
| 3 | every input / tool call / memory write / output in telemetry with provenance | `test_telemetry_covers_all_event_types_with_provenance` |
| 4 | extension points switch on/off without changing behaviour | `test_enabling_passthrough_hooks_changes_nothing` |
| 5 | indirect injection (document asks to read `protected_data.txt`) runs end to end and is logged | task `a2_indirect_injection_pdf`, `test_indirect_injection_logged` |
| 6 | a guard attaches through extension points only | `examples/example_guard.py`, `test_plugin_guard_attaches_without_agent_changes` |
| 7 | clean state with one command | `docker compose up`; sandbox + memory reset before every run (`test_sandbox_resets_between_runs`) |

## Open questions from the PRD, as decided here

* **Agent framework:** a custom lightweight loop (no LangChain dependency, easy to read and to hook).
* **UI:** a single-page console served by FastAPI (no build step). The final dashboard is a separate deliverable.
* **Local model:** start with `qwen2.5:7b` or `llama3.1:8b` on Ollama, depending on the college GPU.
* **Cloud model / budget:** Gemini 2.5 Flash is the cheapest default, capped at USD 2 per process (`budget.max_usd`).
