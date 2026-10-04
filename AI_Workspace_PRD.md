# Product Requirements Document: AI Workspace

**Project:** AgentShield-X (Major Project, UPES School of Computer Science) **Component:** Sandboxed AI Workspace (SRS Objective 1) **Owner:** Sarthak Raghubanshi | **Guide:** Dr. Sushil Ghildiyal **Version:** 0.1 (draft) | **Date:** 04 October 2026

---

## 1. Purpose

The AI Workspace is the controlled environment where a tool-using AI agent runs legitimate tasks and simulated attacks. Per the SRS (Ch. 6-7), it is **the task environment, not a security mechanism**. AgentShield-X (Input, Tool, Memory and Output Guards, policy engine, risk scoring, anomaly detector, provenance tracker) wraps around it and is built by teammates later.

**Goal:** deliver a working, model-agnostic workspace with clean interception points, so a security layer can be plugged in later without modifying agent logic. **No security features are built in this component.**

## 2. Scope

**In scope**

- Agent runtime with a tool-calling loop
- Multi-model support: cloud and local models
- Tool gateway with pluggable tools, including MCP tools
- Docker-isolated sandbox with synthetic data
- Short-term and long-term memory, plus a small RAG component
- Empty pass-through extension points (no security logic) at every stage
- Telemetry emission in the SRS schema (Ch. 9.1)
- Web UI to run tasks, pick models and view logs
- Task and attack runner interface

**Out of scope (handled by teammates)**

- Guard logic, policy engine, risk scoring, provenance logic
- ML/DL anomaly detection, ablation, evaluation
- Final dashboard (the workspace UI is only a task console)

## 3. Users

| User | Need |
| --- | --- |
| Teammates (security/ML) | Hook points, stable telemetry, reproducible runs |
| Evaluators / guide | Clear demo of agent, tools, models and sandbox |
| Future researchers | Reusable, framework-agnostic testbed |

## 4. Functional Requirements

### 4.1 Model integration

- **FR-1:** Support cloud models (e.g., Gemini, OpenAI, Anthropic) via API keys.
- **FR-2:** Support local models (Ollama, vLLM, llama.cpp) via OpenAI-compatible endpoints.
- **FR-3:** Model selectable per task through config or UI, with no code changes.
- **FR-4:** All model calls go through one abstraction layer (LiteLLM), which is also the future Input/Output Guard interception point.
- **FR-5:** Record model name, latency and token usage per call.

### 4.2 Agent and tools

- **FR-6:** Controlled agent loop: task, plan, tool call, observe, repeat, final answer, with a max-step limit.
- **FR-7:** Tools: file read/write, calculator, web search (mock or controlled), mock database, mock email/messaging.
- **FR-8:** MCP support so external tools can be connected without code changes.
- **FR-9:** **Every tool call passes through a single `execute_tool(name, args, context)` gateway.**
- **FR-10:** Each tool declares metadata: name, risk level, resources touched, allowed scope.

### 4.3 Sandbox

- **FR-11:** Agent and tools run inside Docker with restricted network, filesystem and privileges.
- **FR-12:** Pre-loaded synthetic assets: `report.pdf`, `protected_data.txt`, fake API keys, contact list, mock DB. **No real credentials, accounts or data** (SRS Ch. 9.2, 18.2).
- **FR-13:** Sandbox resets to a clean state between runs for reproducibility.

### 4.4 Memory and RAG

- **FR-14:** Short-term (conversation) and long-term (persistent) memory store.
- **FR-15:** Small retrieval component (FAISS or Chroma) over synthetic documents.
- **FR-16:** All memory writes go through one `write_memory()` function with source and trust metadata.

### 4.5 Extension points (no security logic is built now)

- **FR-17:** No guard, policy, risk-scoring or detection logic is built in this component.
- **FR-18:** The agent routes inputs, tool calls, memory writes and outputs through four pass-through functions (`before_input`, `before_tool`, `before_memory_write`, `before_output`) that do nothing by default. A security layer can be attached to them later without changing agent code.
- **FR-19:** Each extension point can be enabled or disabled through config; all are off by default.
- **FR-20:** Content carries a simple `source` tag (`user`, `document`, `retrieved`, `tool_output`, `memory`), recorded in telemetry.

### 4.6 Telemetry

- **FR-21:** Log every event to SQLite (PostgreSQL optional later) in the SRS schema: `timestamp`, `task_id`, `agent_id`, `event_type`, `tool_called/resource_accessed`, `input_guard_result`, `output_guard_result`, `authorisation_result`, `provenance_source`, `trust_level`, `anomaly_score`, `risk_score`, `final_decision`, `outcome`.
- **FR-22:** Security-related columns (guard results, scores, decision) exist in the schema but stay empty for now.
- **FR-23:** Export logs as CSV/JSON for ML training.

### 4.7 Task and attack runner

- **FR-24:** Run tasks from files (YAML/JSON), each labelled `benign` or `attack` with a category.
- **FR-25:** Support the seven SRS attack categories (Ch. 10): direct injection, indirect injection via documents, tool misuse, memory poisoning, RAG poisoning, multi-step behaviour, sensitive output.
- **FR-26:** Record whether the task goal and the attacker goal succeeded (for ASR and task success rate).
- **FR-27:** Support repeated runs per task, since LLM behaviour is probabilistic.

### 4.8 Web UI

- **FR-28:** Choose model, enter or select a task, start a run.
- **FR-29:** Live view of agent steps, tool calls and results.
- **FR-30:** View past runs and export logs.

## 5. Non-Functional Requirements

| ID | Requirement |
| --- | --- |
| NFR-1 | **Modularity:** agent logic contains no security code; the security layer attaches only via hooks |
| NFR-2 | **Model-agnostic:** swapping models or agent frameworks needs minimal change |
| NFR-3 | **Reproducibility:** fixed seeds, versioned configs, clean resets |
| NFR-4 | **Low cost:** open-source stack; paid APIs optional and capped |
| NFR-5 | **Low overhead:** hooks add negligible latency when disabled |
| NFR-6 | **Safety:** attacks run only in the sandbox, with no real external targets |
| NFR-7 | **Portability:** whole workspace starts with `docker compose up` |

## 6. Architecture

```
User / Task Runner
        |
   FastAPI backend
        |
  Agent loop  <-->  LiteLLM  <-->  Cloud / Local models
        |
  [before_input]  [before_memory_write]  [before_output]
        |
  execute_tool()  <--  [before_tool]
        |
  Tools / MCP servers / Memory / RAG   (inside Docker sandbox)
        |
  Telemetry logger  -->  SQLite
```

Hook points are the only places where AgentShield-X attaches. The security layer's decision flows back through the same gateways.

## 7. Tech Stack

| Layer | Choice |
| --- | --- |
| Language | Python |
| Backend | FastAPI |
| Model routing | LiteLLM |
| Local models | Ollama / vLLM / llama.cpp (OpenAI-compatible) |
| Cloud models | Gemini, OpenAI, Anthropic via API keys (optional, capped budget) |
| Agent layer | Custom lightweight tool-calling loop (LangChain optional, TBD) |
| Tool protocol | MCP (Python SDK) plus native tools |
| Retrieval | FAISS or Chroma |
| Memory | SQLite or JSON store |
| Telemetry DB | SQLite (PostgreSQL if volume requires) |
| Sandbox | Docker, Docker Compose |
| UI | Streamlit (MVP) or React |
| Data tools | Pandas, NumPy |
| Version control | Git / GitHub |

Choices marked TBD are finalised in the design phase, in line with SRS Ch. 14.

## 8. Milestones (aligned with SRS PERT, activities B and C)

| Milestone | Deliverable |
| --- | --- |
| M1 | Model layer: cloud and local models through LiteLLM |
| M2 | Agent loop and `execute_tool` gateway with core tools |
| M3 | Docker sandbox with synthetic data and reset |
| M4 | Memory, RAG, MCP integration |
| M5 | Extension points, source tags, telemetry logger |
| M6 | UI, task runner, sample benign and attack tasks |
| M7 | Integration handover and docs for teammates |

## 9. Acceptance Criteria

1. The same task runs on at least one cloud model and one local model by changing only config.
2. An agent completes a legitimate multi-tool task inside the Docker sandbox.
3. Every tool call, memory write, input and output appears in telemetry with provenance.
4. All four extension points exist as pass-through functions and can be switched on or off without changing agent behaviour.
5. A sample indirect-injection task (document asks the agent to read `protected_data.txt`) runs end to end and is logged.
6. A teammate could later attach a guard using only these extension points, without editing agent code.
7. Workspace starts from a clean state with one command.

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| Small local models call tools unreliably | Test early; use a function-calling-capable model; fall back to a cloud model |
| Sandbox escape or misconfiguration | Restricted container, no real credentials, no external network by default |
| Extension-point signatures change late and break teammates' work | Freeze the extension-point signatures by M5 |
| Telemetry schema mismatch with ML work | Agree the schema with the ML teammates before M5 |
| Time pressure before mid-term | Ship M1-M3 first; MCP and RAG are secondary |

## 11. Open Questions

- Which agent framework: custom loop or LangChain?
- Which local model fits the college GPU?
- Which cloud model(s), and what API budget cap?
- Streamlit or React for the UI?

## 12. References

SRS: *AgentShield-X: A Data-Driven Security & Reliability Framework for Autonomous AI Agents*, 01 September 2026, Ch. 5-10, 12, 14-15.