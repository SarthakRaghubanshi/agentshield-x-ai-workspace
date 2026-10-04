"""Live tests against REAL models (PRD acceptance criterion 1). Skipped unless configured.

    set AIWORKSPACE_LIVE_MODELS=gemini-flash,ollama-qwen2.5     (comma separated model ids)
    pytest -q tests/test_live_model.py -s

Each model runs the same benign multi-tool task and the indirect-injection task, changing only
the model id. The test asserts that the run completes and is fully logged - not whether the
model resisted the attack (that is an experimental result, not a test outcome).
"""
from __future__ import annotations

import os

import pytest

from aiworkspace.config import deep_merge, workspace_config
from aiworkspace.sandbox.client import LocalSandbox
from aiworkspace.workspace import Workspace

MODELS = [m.strip() for m in os.environ.get("AIWORKSPACE_LIVE_MODELS", "").split(",") if m.strip()]
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(not MODELS, reason="set AIWORKSPACE_LIVE_MODELS to run")]


@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("task_id", ["meeting_followup", "a2_indirect_injection_pdf"])
async def test_same_task_on_real_model(tmp_path, model, task_id):
    cfg = deep_merge(workspace_config(), {"telemetry": {"db_path": str(tmp_path / "t.db")},
                                          "memory": {"db_path": str(tmp_path / "m.db")}, "hooks": {"plugins": []}})
    ws = Workspace(cfg, sandbox=LocalSandbox(root=tmp_path / "sandbox", enable_mcp=False))
    await ws.start()
    try:
        run = await ws.run(task_id=task_id, model=model)
        events = ws.telemetry.events(run["run_id"])
        print(f"\n[{model}] {task_id}: status={run['status']} task_success={run['task_success']} "
              f"attack_success={run['attack_success']} steps={run['steps']}\n{run['final_output']}")
        assert run["status"] in ("completed", "max_steps"), run["final_output"]
        assert any(e["event_type"] == "model_call" and e["total_tokens"] for e in events)
        assert any(e["event_type"] == "tool_call" for e in events), "model made no tool calls"
    finally:
        await ws.stop()
