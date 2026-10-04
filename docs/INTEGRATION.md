# Integration guide for the AgentShield-X security layer

This guide is for the teammates building the guards, policy engine, risk scoring, provenance
tracker and anomaly detector. The AI Workspace contains **no security logic**. You attach to it
only through the four extension points below, and you never edit `aiworkspace/agent.py`,
`gateway.py` or `memory.py` (PRD NFR-1, acceptance criterion 6).

## 1. The four extension points (frozen signatures)

| Point | Fires on | `payload` | `ctx.source` |
|---|---|---|---|
| `before_input` | the user prompt; every tool output before the model reads it; every long-term memory recalled into the prompt | `str` | `user`, `document`, `retrieved`, `tool_output`, `memory` |
| `before_tool` | every tool call, before it executes (inside `execute_tool`) | `{"name", "args", "resource"}` | `agent` |
| `before_memory_write` | every long-term memory write (inside `write_memory`) | `{"content", "source", "trust_level", "metadata"}` | the content's source |
| `before_output` | the final answer, before it is released | `str` | `agent` |

Handler signature (sync or async):

```python
def handler(payload, ctx: HookContext) -> HookResult | None
```

* Return `None` to allow the action unchanged.
* `HookResult(decision=Decision.BLOCK, reason=...)` stops the action. The agent gets a
  "[... blocked by the security layer: reason]" message instead, and the event is logged.
* `HookResult(decision=Decision.REVIEW)` holds the action (treated as BLOCK unless
  `hooks.review_behaviour: allow`).
* `HookResult(payload=new_payload)` replaces the payload, e.g. a redacted output or rewritten args.
* Several handlers on one point run in registration order. BLOCK short-circuits the chain.
  If a handler raises, the error is recorded and the action continues
  (set `hooks.on_error: block` to fail closed).

### What `HookContext` gives you

`run_id`, `task_id`, `agent_id`, `step`, `model`, `source` (provenance tag), `trust_level`
(default trust for that source, from `config/workspace.yaml`), `task` (label, category, prompt and
the task's **`authorised` tools/resources**: the intended scope for a Tool Guard), `tool`
(FR-10 metadata: `risk_level`, `resources`, `scope`, `output_source`, ...), and `trace`
(the list of earlier actions in the run, for sequence-based anomaly detection and provenance).

### Filling the telemetry columns

Whatever you put in `HookResult` lands in the SRS telemetry columns of that event:

| `HookResult` field | telemetry column |
|---|---|
| `guard_result` | `input_guard_result` (before_input) / `output_guard_result` (before_output) |
| `authorisation_result` | `authorisation_result` |
| `trust_level` | `trust_level` (overrides the default) |
| `risk_score` | `risk_score` (max across handlers) |
| `anomaly_score` | `anomaly_score` |
| `decision` | `final_decision` (ALLOW / BLOCK / REVIEW) |
| `reason`, `metadata` | `details_json.hook` |

## 2. Attaching a plugin (no code changes)

```python
# agentshield/guards.py
from aiworkspace.hooks import Decision, HookResult

def tool_guard(payload, ctx):
    ...
    return HookResult(decision=Decision.BLOCK, authorisation_result="DENY", risk_score=0.9, reason="...")

def register(manager):
    manager.register("before_tool", tool_guard)
    manager.set_enabled("before_tool", True)
```

Enable it in any one of these ways:

* `config/workspace.yaml` → `hooks.plugins: ["agentshield.guards:register"]`
* environment: `AGENTSHIELD_PLUGINS=agentshield.guards:register` (also works with docker compose)
* CLI: `python -m aiworkspace batch --plugin agentshield.guards:register`

Each point can also be switched on/off in `hooks.enabled`, from the UI, or with `--enable`.
Every run records the active configuration in `runs.defence` (e.g. `baseline` or
`before_tool:tool_guard`), so baseline vs defended results never get mixed (SRS Ch. 12).

`examples/example_guard.py` is a 20-line working example: a scope check against the task's
`authorised.resources`.

## 3. Telemetry for the ML work

* SQLite at `runtime/telemetry.db`, tables `events` and `runs`.
* Export: UI buttons, `GET /api/export?fmt=csv|json&table=events|runs`, or
  `python -m aiworkspace export --fmt csv --out events.csv`.
* `events` = SRS Ch. 9.1 fields + `run_id, step, label, category, model, latency_ms,
  prompt_tokens, completion_tokens, total_tokens, cost_usd, args_json, content, details_json`.
* `event_type` values: `task_start, input_received, memory_read, model_call, tool_call,
  tool_result, memory_write, output_generated, task_end, error`.
* `runs` holds `label` (benign/attack), `category`, `defence`, `repeat_index`, `batch_id`,
  `task_success`, `attack_success` and `evaluation_json` (per-check results, tool calls,
  blocked calls, calls outside the authorised scope).
* `GET /api/summary` computes the SRS Ch. 13 metrics (ASR, task success, unsafe action, tool
  misuse, recovery, false blocking, latency overhead) per model × defence × category.
* Every event where a hook ran has `details_json.hook.hook_ms`, the time spent in your handlers.

## 4. Adding tasks and attacks

Drop a YAML file in `tasks/benign/` or `tasks/attacks/`. The format is documented at the top
of `aiworkspace/tasks.py`. `success.task` checks must all pass for task success. Any passing
`success.attack` check means the attacker's goal was reached. `authorised.resources` is the
task's intended scope: the workspace only *measures* calls outside it (tool misuse rate);
enforcing it is the Tool Guard's job.

## 5. Rules of the testbed

All data is synthetic (`sandbox_seed/`). Email/messages go to an outbox file. Web search and
fetch use local fixtures. In Docker the tools run in a container with no internet access.
Never add real credentials, accounts or external targets (SRS Ch. 9.2, 18.2).
