"""Tests mapped to the PRD acceptance criteria (section 9). Run: pytest -q"""
from __future__ import annotations

import csv
import io
import json

import pytest

from aiworkspace.config import deep_merge, workspace_config
from aiworkspace.hooks import Decision, HookContext, HookManager, HookResult, build_manager
from aiworkspace.sandbox.client import LocalSandbox
from aiworkspace.telemetry import SRS_COLUMNS
from aiworkspace.workspace import Workspace

from .scripted_model import ScriptedModelClient

pytestmark = pytest.mark.asyncio


def make_cfg(tmp_path, **override):
    return deep_merge(workspace_config(), deep_merge({
        "telemetry": {"db_path": str(tmp_path / "telemetry.db")},
        "memory": {"db_path": str(tmp_path / "memory.db")},
        "hooks": {"plugins": []},
    }, override))


@pytest.fixture
async def ws(tmp_path):
    cfg = make_cfg(tmp_path)
    w = Workspace(cfg, sandbox=LocalSandbox(root=tmp_path / "sandbox", enable_mcp=False))
    await w.start()
    if w.telemetry.backend == "postgresql":  # CI runs the suite against PostgreSQL too
        w.telemetry.clear()
    w.agent.models = ScriptedModelClient(w.tasks)
    yield w
    await w.stop()


def ctx():
    return HookContext(point="", run_id="r", task_id="t", agent_id="a", step=0, source="user", trust_level="trusted")


# ---- extension points -------------------------------------------------------------
async def test_hooks_are_passthrough_and_off_by_default():
    manager = build_manager(workspace_config())
    for point in ("before_input", "before_tool", "before_memory_write", "before_output"):
        assert manager.enabled[point] is False
        out = await manager.dispatch(point, {"x": 1}, ctx())
        assert out.allowed and out.payload == {"x": 1} and not out.hooks_ran


async def test_hook_chain_modify_then_block():
    m = HookManager(enabled={"before_output": True})
    m.register("before_output", lambda p, c: HookResult(payload=p.replace("secret", "[redacted]")))
    m.register("before_output", lambda p, c: HookResult(decision=Decision.BLOCK, reason="nope", guard_result="flagged"))
    m.register("before_output", lambda p, c: pytest.fail("chain must stop at BLOCK"))
    out = await m.dispatch("before_output", "a secret", ctx())
    assert out.decision == Decision.BLOCK and out.payload == "a [redacted]" and out.guard_result == "flagged"


async def test_broken_hook_does_not_crash_agent():
    m = HookManager(enabled={"before_input": True})
    m.register("before_input", lambda p, c: 1 / 0)
    out = await m.dispatch("before_input", "hi", ctx())
    assert out.allowed and out.metadata["errors"]


# ---- acceptance 2: legitimate multi-tool task completes in the sandbox ----------------
async def test_benign_tasks_succeed(ws):
    from .scripted_model import TRACES

    benign = [t for t, v in ws.tasks.items() if v["label"] == "benign" and t in TRACES and t != "calendar_mcp"]
    assert len(benign) == 10
    result = await ws.run_batch(benign, model="test/scripted")
    failures = [(r["task_id"], r["status"], r["evaluation_json"]) for r in result["runs"] if r["task_success"] != 1]
    assert not failures


# ---- acceptance 3: every input, tool call, memory write and output is in telemetry ------
async def test_telemetry_covers_all_event_types_with_provenance(ws):
    run = await ws.run(task_id="a4_memory_poisoning", model="test/scripted")
    events = ws.telemetry.events(run["run_id"])
    types = {e["event_type"] for e in events}
    assert {"task_start", "input_received", "memory_write", "memory_read", "model_call",
            "tool_call", "tool_result", "output_generated", "task_end"} <= types
    for e in events:
        if e["event_type"] in ("input_received", "tool_call", "tool_result", "memory_write", "memory_read", "output_generated"):
            assert e["provenance_source"] and e["trust_level"], e
    seeded = next(e for e in events if e["event_type"] == "memory_write")
    assert seeded["provenance_source"] == "document" and seeded["trust_level"] == "untrusted"


async def test_export_has_srs_schema(ws):
    await ws.run(task_id="growth_calculation", model="test/scripted")
    header = next(csv.reader(io.StringIO(ws.telemetry.export("csv"))))
    assert all(col in header for col in SRS_COLUMNS)
    assert json.loads(ws.telemetry.export("json", "runs"))[0]["task_id"] == "growth_calculation"


# ---- acceptance 4: switching extension points on/off does not change behaviour ---------
async def test_enabling_passthrough_hooks_changes_nothing(ws):
    off = await ws.run(task_id="meeting_followup", model="test/scripted")
    for point in ws.hooks.enabled:
        ws.hooks.set_enabled(point, True)
    ws.hooks.register("before_tool", lambda p, c: None)  # a do-nothing handler
    on = await ws.run(task_id="meeting_followup", model="test/scripted")
    assert off["final_output"] == on["final_output"] and off["task_success"] == on["task_success"] == 1
    strip = lambda evs: [(e["event_type"], e["tool_called"], e["outcome"]) for e in evs]
    assert strip(ws.telemetry.events(off["run_id"])) == strip(ws.telemetry.events(on["run_id"]))


# ---- acceptance 5: indirect injection runs end to end and is logged ---------------------
async def test_indirect_injection_logged(ws):
    run = await ws.run(task_id="a2_indirect_injection_pdf", model="test/scripted")
    assert run["status"] == "completed" and run["attack_success"] == 1
    events = ws.telemetry.events(run["run_id"])
    results = [e for e in events if e["event_type"] == "tool_result"]
    assert results[0]["resource_accessed"] == "file:supplier_report.pdf"
    assert results[0]["provenance_source"] == "document"
    assert any(e["resource_accessed"] == "file:protected_data.txt" for e in results)


# ---- acceptance 6: a guard attaches through the extension points only ------------------
async def test_plugin_guard_attaches_without_agent_changes(ws):
    ws.hooks.load_plugin("examples.example_guard:register")
    run = await ws.run(task_id="a3_tool_misuse", model="test/scripted")
    assert run["attack_success"] == 0 and run["task_success"] == 1
    denied = [e for e in ws.telemetry.events(run["run_id"]) if e["authorisation_result"] == "DENY"]
    assert denied and denied[0]["final_decision"] == "BLOCK" and denied[0]["risk_score"] == 0.9


async def test_memory_write_passes_extension_point(ws):
    ws.hooks.set_enabled("before_memory_write", True)
    ws.hooks.register("before_memory_write", lambda p, c: HookResult(decision=Decision.BLOCK, reason="quarantine")
                      if p["trust_level"] == "untrusted" else None)
    run = await ws.run(task_id="a4_memory_poisoning", model="test/scripted")
    write = next(e for e in ws.telemetry.events(run["run_id"]) if e["event_type"] == "memory_write")
    assert write["outcome"] == "blocked" and ws.memory.all() == []


# ---- acceptance 7 / FR-13: clean state between runs ---------------------------------
async def test_sandbox_resets_between_runs(ws):
    await ws.run(task_id="meeting_followup", model="test/scripted")
    assert len((await ws.sandbox.state())["emails_sent"]) == 1
    await ws.run(task_id="growth_calculation", model="test/scripted")
    assert (await ws.sandbox.state())["emails_sent"] == []


async def test_repeated_runs_are_recorded(ws):
    result = await ws.run_batch(["growth_calculation"], model="test/scripted", repeats=3)
    assert [r["repeat_index"] for r in result["runs"]] == [0, 1, 2]
    assert result["summary"]["groups"][0]["runs"] == 3


# ---- FR-8: MCP tools are registered without code changes ------------------------------
async def test_mcp_calendar(tmp_path):
    w = Workspace(make_cfg(tmp_path), sandbox=LocalSandbox(root=tmp_path / "sandbox"))
    await w.start()
    w.agent.models = ScriptedModelClient(w.tasks)
    try:
        assert "mcp__calendar__create_event" in w.gateway.specs
        run = await w.run(task_id="calendar_mcp", model="test/scripted")
        assert run["task_success"] == 1
    finally:
        await w.stop()


# ---- SRS Ch. 13 inputs: scope measurement, hook latency -------------------------------
async def test_out_of_scope_calls_are_measured_not_blocked(ws):
    run = await ws.run(task_id="a3_tool_misuse", model="test/scripted")
    evaluation = json.loads(run["evaluation_json"])
    assert evaluation["tool_calls_outside_scope"] == 1 and evaluation["executed_outside_scope"] == 1
    assert evaluation["outside_scope_resources"] == ["file:protected_data.txt"]
    metrics = ws.telemetry.summary()["groups"][0]
    assert metrics["tool_misuse_rate"] == 0.5 and metrics["unsafe_action_rate"] == 1.0


async def test_hook_latency_is_recorded(ws):
    ws.hooks.load_plugin("examples.example_guard:register")
    run = await ws.run(task_id="summarise_report", model="test/scripted")
    call = next(e for e in ws.telemetry.events(run["run_id"]) if e["event_type"] == "tool_call")
    assert json.loads(call["details_json"])["hook"]["hook_ms"] >= 0
    assert run["defence"] == "before_tool:scope_guard"


async def test_task_files_cover_all_srs_attack_categories(ws):
    from aiworkspace.tasks import CATEGORIES

    attack_categories = {t["category"] for t in ws.tasks.values() if t["label"] == "attack"}
    assert attack_categories == set(CATEGORIES) - {"benign"}


# ---- NFR-5: disabled extension points add negligible latency ---------------------------
async def test_disabled_hooks_overhead_is_negligible():
    import time

    manager = build_manager(workspace_config())  # all points off
    n = 20000
    start = time.perf_counter()
    for _ in range(n):
        await manager.dispatch("before_tool", {"name": "x"}, ctx())
    per_call_us = (time.perf_counter() - start) / n * 1e6
    assert per_call_us < 20, f"{per_call_us:.2f} µs per disabled hook call"


# ---- FR-24: task files may be JSON as well as YAML ------------------------------------
async def test_json_task_files(tmp_path):
    from aiworkspace.tasks import load_task_file

    path = tmp_path / "t.json"
    path.write_text(json.dumps([{"id": "j1", "label": "benign", "prompt": "hi"},
                                {"id": "j2", "label": "attack", "category": "tool_misuse", "prompt": "x"}]))
    tasks = load_task_file(path)
    assert [t["id"] for t in tasks] == ["j1", "j2"] and tasks[1]["category"] == "tool_misuse"
