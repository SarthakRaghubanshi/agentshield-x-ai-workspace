# AgentShield-X: AI Workspace

[![ci](https://github.com/Kroszborg/agentshield-x-ai-workspace/actions/workflows/ci.yml/badge.svg)](https://github.com/Kroszborg/agentshield-x-ai-workspace/actions/workflows/ci.yml)

The sandboxed task environment for **AgentShield-X** (SRS Objective 1, see
[`AI_Workspace_PRD.md`](AI_Workspace_PRD.md) and `AgentShield-X_SRS.pdf`).

A tool-using AI agent runs legitimate tasks and controlled attacks inside a sandbox. Every
input, tool call, memory write and output is logged as structured telemetry in the SRS
Ch. 9.1 schema. The AgentShield-X security layer (guards, policy engine, risk scoring,
provenance, anomaly detection) plugs in later through four extension points, without
touching agent code.

> This component contains **no security logic** (PRD FR-17). The teammates' integration guide
> is [`docs/INTEGRATION.md`](docs/INTEGRATION.md).

## Documentation

| Guide | For |
|---|---|
| [**Setup guide**](docs/SETUP.md) | Installing and running on **Windows, macOS and Linux**, with or without Docker, plus model setup (Ollama, vLLM, llama.cpp, cloud APIs) |
| [**User guide**](docs/USER_GUIDE.md) | Using the web console and CLI: running tasks and attacks, reading results and metrics, exporting telemetry, writing tasks, adding tools / MCP servers / models |
| [**Testing guide**](docs/TESTING.md) | Checking it yourself: automated tests, a console walkthrough per PRD acceptance criterion, Docker isolation checks, PRD / SRS compliance tables |
| [**Integration guide**](docs/INTEGRATION.md) | Security / ML teammates: extension-point contract, plugins, telemetry columns |

## Quick start

You need **Python 3.12 or 3.13**, **git** and at least one model (local [Ollama](https://ollama.com) is free).

**Windows (PowerShell)**
```powershell
git clone https://github.com/Kroszborg/agentshield-x-ai-workspace.git
cd agentshield-x-ai-workspace
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
ollama pull qwen3:4b-instruct
python -m aiworkspace serve        # open http://localhost:8000
```

**macOS / Linux**
```bash
git clone https://github.com/Kroszborg/agentshield-x-ai-workspace.git
cd agentshield-x-ai-workspace
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
ollama pull qwen3:4b-instruct
python -m aiworkspace serve        # open http://localhost:8000
```

**Docker (any OS, PRD NFR-7)**
```bash
docker compose up --build          # open http://localhost:8000
```

Online models instead of (or as well as) Ollama: copy `.env.example` to `.env` and set a key:
`GEMINI_API_KEY` or `GROQ_API_KEY` (both have free tiers), `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
`OPENROUTER_API_KEY` or `MISTRAL_API_KEY`. Full details are in the
[setup guide](docs/SETUP.md).

## Features

* **Web app:** a **Chat** screen to talk to the agent (conversations, recent chats, steps behind
  each answer, approvals inline) and an **Experiments** screen for tasks, attack suites, extension
  points and metrics.
* **Agent:** tool-calling loop with a step limit, multi-turn conversations (short-term memory),
  chat in the browser or with `python -m aiworkspace chat`, long-term memory with
  provenance, and recovery of tool calls that small models write as plain text.
* **Models:** any LiteLLM model: Ollama, vLLM, llama.cpp, Gemini, Groq, OpenAI, Anthropic, OpenRouter, Mistral. Choose
  per run, no code changes, with a budget cap on paid APIs.
* **Tools:** 12 sandbox tools with risk / scope metadata, plus MCP servers over stdio or
  Streamable HTTP. Every call goes through one `execute_tool()` gateway.
* **Sandbox:** synthetic data, a clean reset before every run, and under Docker a separate
  container with no internet, a read-only image, a non-root user and dropped capabilities.
* **Extension points:** four pass-through points for the security layer, switchable at runtime.
  REVIEW decisions can be held, allowed, or sent to a human Approve / Reject queue in the console.
* **Telemetry:** SRS Ch. 9.1 schema in SQLite or PostgreSQL, live streaming, CSV / JSON export,
  and SRS Ch. 13 metrics per model × defence configuration.

## Architecture

```
 Web console / CLI / task files
            |
      FastAPI backend ─────────────── telemetry (SQLite, SRS Ch. 9.1) ── CSV / JSON export
            |
       Agent loop  <──>  LiteLLM  <──>  Ollama / vLLM / llama.cpp / Gemini / OpenAI / Anthropic
            |
   [before_input] [before_memory_write] [before_output]     <- extension points (off by default)
            |
   execute_tool()  <── [before_tool]
            |
   Sandbox: 12 native tools + MCP servers + RAG + synthetic data   (separate container, no internet)
```

| Docker service | Role | Network |
|---|---|---|
| `workspace` | agent loop, LiteLLM, API + console, telemetry, memory | internal + egress (model APIs) |
| `sandbox` | all tools, RAG index, MCP servers, synthetic data | **internal only (no internet)**; read-only image, non-root, all capabilities dropped, ephemeral storage restored before every run |
| `ollama` (optional profile) | local models | egress |

## Repository layout

```
aiworkspace/
  agent.py        agent loop: task → model → tool call → observe → … → final answer (FR-6)
  models.py       ModelClient: every model call goes through LiteLLM (FR-1..5)
  gateway.py      execute_tool(name, args, context): the single tool gateway (FR-9)
  memory.py       long-term memory; write_memory() is the only write path (FR-14, FR-16)
  hooks.py        the four pass-through extension points (FR-17..19)
  telemetry.py    SQLite logger, live streaming, metrics, CSV/JSON export (FR-21..23)
  tasks.py        task files + task / attack success evaluation (FR-24..26)
  workspace.py    orchestration: reset → seed → run → evaluate → log; batches and repeats
  api.py, ui/     FastAPI backend + web console (FR-28..30)
  sandbox/        tools with FR-10 metadata, FAISS RAG, MCP bridge, reset, sandbox HTTP server
config/           workspace.yaml, models.yaml, mcp_servers.yaml
sandbox_seed/     synthetic data: report.pdf, protected_data.txt, fake keys, contacts, DB, KB, inbox, web pages
tasks/            16 benign tasks (incl. multi-turn) + 7 attack tasks (one per SRS Ch. 10 category)
attack_assets/    task-specific attack content, copied into the sandbox only for the task that uses it
mcp_servers/      sample MCP server (calendar)
examples/         example_guard.py: how a guard plugs in (example only)
tests/            acceptance tests (+ optional live-model tests)
docs/             setup, user and integration guides
```

## PRD acceptance criteria

| # | Criterion | Where |
|---|---|---|
| 1 | same task on a cloud and a local model by config only | `--model gemini-flash` vs `--model ollama-qwen3-4b`; `tests/test_live_model.py`; the `live-model` workflow |
| 2 | legitimate multi-tool task in the sandbox | `meeting_followup`, `calendar_mcp`, … (`test_benign_tasks_succeed`) |
| 3 | every input / tool call / memory write / output in telemetry with provenance | `test_telemetry_covers_all_event_types_with_provenance` |
| 4 | extension points switch on/off without changing behaviour | `test_enabling_passthrough_hooks_changes_nothing` |
| 5 | indirect injection (document asks to read `protected_data.txt`) runs end to end and is logged | task `a2_indirect_injection_pdf`, `test_indirect_injection_logged` |
| 6 | a guard attaches through extension points only | `examples/example_guard.py`, `test_plugin_guard_attaches_without_agent_changes` |
| 7 | clean state with one command | `docker compose up`; reset before every run; CI checks the Docker isolation |

`tests/test_workspace.py` exercises the plumbing with a test-only scripted model client
(`tests/scripted_model.py`, never used by the workspace). `tests/test_live_model.py` runs the
same tasks on real models when `AIWORKSPACE_LIVE_MODELS` is set.

## Verified with a real model

`qwen3:4b-instruct` on Ollama (laptop RTX 4050), single run per task:

| Configuration | Benign task success | Attack categories where the attack succeeded |
|---|---|---|
| baseline (no defence) | 16 / 16 | 4 / 7: direct injection, indirect injection (PDF), tool misuse, sensitive output |
| `before_tool` example scope guard | (not re-run) | 2 / 7: direct injection, sensitive output (need Input / Output Guards) |

These are single runs, so they are illustrations, not results. Use `--repeats` for experiments.
The `live-model` GitHub workflow repeats the check on a CPU runner, including a task run inside Docker.

## PRD open questions, as decided

* **Agent framework:** a custom lightweight tool-calling loop (no LangChain), easy to read and to hook.
* **UI:** a single-page console served by FastAPI (no build step). The final dashboard is a separate deliverable.
* **Local model:** `qwen3:4b-instruct` (2.5 GB, runs on a laptop) by default; `qwen2.5:7b` / `llama3.1:8b` or any model behind vLLM / llama.cpp on the college GPU.
* **Cloud model / budget:** Gemini 2.5 Flash as the low-cost option. Paid spend is capped at USD 2 per process (`budget.max_usd`).
