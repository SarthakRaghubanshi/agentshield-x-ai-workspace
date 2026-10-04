"""Tests for conversations, small-model robustness, human review, MCP over HTTP, task assets and the API."""
from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest

from aiworkspace.agent import clean_answer, text_tool_calls
from aiworkspace.hooks import Decision, HookResult
from aiworkspace.review import ReviewQueue
from aiworkspace.sandbox.client import LocalSandbox
from aiworkspace.sandbox.mcp_bridge import MCPBridge
from aiworkspace.workspace import Workspace

from .scripted_model import ScriptedModelClient
from .test_workspace import make_cfg

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ws(tmp_path):
    w = Workspace(make_cfg(tmp_path), sandbox=LocalSandbox(root=tmp_path / "sandbox", enable_mcp=False))
    await w.start()
    if w.telemetry.backend == "postgresql":
        w.telemetry.clear()
    w.agent.models = ScriptedModelClient(w.tasks)
    yield w
    await w.stop()


# ---- conversations (FR-14) ---------------------------------------------------------
async def test_multi_turn_task_keeps_short_term_memory(ws):
    run = await ws.run(task_id="followup_report", model="test/scripted")
    assert run["task_success"] == 1 and run["turns"] == 2
    assert "[turn 1]" in run["final_output"] and "[turn 2]" in run["final_output"]
    turn2_call = ws.agent.models.seen[-1]
    assert any("48.2" in str(m.get("content")) for m in turn2_call), "turn 2 must see turn 1"
    inputs = [e for e in ws.telemetry.events(run["run_id"]) if e["event_type"] == "input_received"]
    assert [e["turn"] for e in inputs] == [1, 2]


async def test_new_conversation_keeps_only_long_term_memory(ws):
    run = await ws.run(task_id="preference_across_sessions", model="test/scripted")
    assert run["task_success"] == 1
    events = ws.telemetry.events(run["run_id"])
    second_input = [e for e in events if e["event_type"] == "input_received"][1]
    assert json.loads(second_input["details_json"])["history_messages"] == 0
    recalled = [e for e in events if e["event_type"] == "memory_read" and e["turn"] == 2]
    assert recalled and "three bullet" in recalled[0]["content"]
    # the memory row records what the agent had read before writing (nothing yet, in turn 1)
    assert json.loads(ws.memory.all()[0]["metadata_json"]) == {"context_sources": []}


async def test_console_follow_up_continues_session(ws):
    first = await ws.run(task_id="meeting_followup", model="test/scripted")
    assert len((await ws.sandbox.state())["emails_sent"]) == 1
    follow = await ws.run(prompt="Thanks. Anything else?", model="test/scripted", session_id=first["session_id"])
    assert follow["session_id"] == first["session_id"]
    # no reset: the email sent in the first run is still in the outbox
    assert len((await ws.sandbox.state())["emails_sent"]) == 1
    last_call = ws.agent.models.seen[-1]
    assert any(m["role"] == "tool" for m in last_call), "history from the first run is in context"
    start = next(e for e in ws.telemetry.events(follow["run_id"]) if e["event_type"] == "task_start")
    assert json.loads(start["details_json"])["continues_session"] is True


# ---- small-model robustness ----------------------------------------------------------
async def test_text_tool_calls_are_recovered():
    names = {"read_file", "calculator"}
    assert text_tool_calls('{"name": "read_file", "arguments": {"path": "a.txt"}}', names)[0]["arguments"] == {"path": "a.txt"}
    assert text_tool_calls('```json\n{"name": "calculator", "parameters": {"expression": "1+1"}}\n```', names)[0]["name"] == "calculator"
    tagged = text_tool_calls('<tool_call>{"name": "read_file", "arguments": "{\\"path\\": \\"b\\"}"}</tool_call>', names)
    assert tagged[0]["arguments"] == {"path": "b"}
    assert text_tool_calls('{"name": "delete_everything", "arguments": {}}', names) == []
    assert text_tool_calls("The answer is 42.", names) == []
    assert clean_answer("<think>hmm</think>\nHello") == "Hello"


# ---- human review (REVIEW decisions) ----------------------------------------------------
@pytest.mark.parametrize("approve", [True, False])
async def test_human_review_approve_and_reject(ws, approve):
    queue = ReviewQueue(ws.telemetry, timeout_s=5)
    ws.hooks.review_behaviour = "human"
    ws.hooks.reviewer = queue.request
    ws.hooks.set_enabled("before_tool", True)
    ws.hooks.register("before_tool", lambda p, c: HookResult(decision=Decision.REVIEW, reason="email leaves the org")
                      if p["name"] == "send_email" else None)

    async def reviewer():
        while not queue.list():
            await asyncio.sleep(0.01)
        assert queue.list()[0]["point"] == "before_tool"
        queue.resolve(queue.list()[0]["review_id"], approve)

    run, _ = await asyncio.gather(ws.run(task_id="meeting_followup", model="test/scripted"), reviewer())
    assert run["task_success"] == (1 if approve else 0)
    call = next(e for e in ws.telemetry.events(run["run_id"]) if e["tool_called"] == "send_email" and e["event_type"] == "tool_call")
    assert call["final_decision"] == ("ALLOW" if approve else "BLOCK")
    assert json.loads(call["details_json"])["hook"]["human_review"]["approved"] is approve


async def test_review_without_reviewer_blocks(ws):
    ws.hooks.review_behaviour = "human"
    ws.hooks.set_enabled("before_tool", True)
    ws.hooks.register("before_tool", lambda p, c: HookResult(decision=Decision.REVIEW))
    run = await ws.run(task_id="growth_calculation", model="test/scripted")
    call = next(e for e in ws.telemetry.events(run["run_id"]) if e["event_type"] == "tool_call")
    assert call["final_decision"] == "REVIEW" and call["outcome"] == "review"


# ---- task assets kept out of the shared seed ----------------------------------------------
async def test_attack_assets_only_in_their_task(ws):
    await ws.run(task_id="a5_rag_poisoning", model="test/scripted")
    assert "faq_account_recovery_update.md" in (await ws.sandbox.execute("search_knowledge_base", {"query": "account recovery card"}))["output"]
    await ws.run(task_id="refund_policy_kb", model="test/scripted")
    assert "faq_account_recovery_update.md" not in (await ws.sandbox.execute("search_knowledge_base", {"query": "account recovery card"}))["output"]


# ---- MCP over Streamable HTTP (FR-8) ------------------------------------------------------
async def test_mcp_server_over_http(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "mcp_servers/calendar_server.py", "--http", str(port)],
                            env={**__import__("os").environ, "SANDBOX_ROOT": str(tmp_path)},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        bridge = MCPBridge({"cal": {"url": f"http://127.0.0.1:{port}/mcp", "risk_level": "low"}}, tmp_path)
        await bridge.start()
        assert not bridge.errors and "mcp__cal__create_event" in bridge.specs
        out = await bridge.call("mcp__cal__create_event", {"title": "HTTP test", "date": "2026-10-10", "time": "09:00"})
        assert "HTTP test" in out
        await bridge.stop()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


# ---- the HTTP API used by the console (FR-28..30) ---------------------------------------------
async def test_api_run_follow_up_and_export(ws):
    from aiworkspace import api

    api.ws = ws
    api.reviews = ReviewQueue(ws.telemetry, timeout_s=5)
    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/api/health")).json()["tools"] == 14
        tasks = (await client.get("/api/tasks")).json()
        assert len(tasks) == 23 and {t["label"] for t in tasks} == {"benign", "attack"}
        async def wait(run_id):
            for _ in range(500):
                r = await client.get(f"/api/runs/{run_id}")
                if r.status_code == 200 and r.json()["run"]["status"] not in ("queued", "running"):
                    return r.json()["run"]
                await asyncio.sleep(0.02)
            raise AssertionError("run did not finish")

        started = (await client.post("/api/runs", json={"task_id": "summarise_report", "model": "test/scripted"})).json()
        run = await wait(started["run_ids"][0])
        assert run["task_success"] == 1
        follow = (await client.post("/api/runs", json={"prompt": "Thanks", "model": "test/scripted",
                                                       "session_id": run["session_id"]})).json()
        await wait(follow["run_ids"][0])
        session = (await client.get(f"/api/sessions/{run['session_id']}")).json()
        assert len(session["runs"]) == 2 and session["messages"][-1]["role"] == "assistant"
        csv_text = (await client.get("/api/export?fmt=csv&table=events")).text
        assert csv_text.startswith("event_id,timestamp,task_id")
        assert (await client.post("/api/review-mode", json={"behaviour": "human"})).json()["review_behaviour"] == "human"
        assert (await client.post("/api/reviews/rev-missing", json={"approve": True})).status_code == 404


# ---- chat screen support --------------------------------------------------------------------
async def test_chat_sessions_memory_and_interrupted_runs(ws):
    from aiworkspace import api

    api.ws = ws
    api.reviews = ReviewQueue(ws.telemetry, timeout_s=5)
    # a chat that saves a note, then a NEW chat that keeps long-term memory but gets a clean sandbox
    first = await ws.run(task_id=None, prompt="Remember this preference for future conversations: my summaries should always be exactly three bullet points.",
                         model="test/scripted")
    second = await ws.run(prompt="Hello again", model="test/scripted")
    assert first["session_id"] != second["session_id"]

    transport = httpx.ASGITransport(app=api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        sessions = (await client.get("/api/sessions")).json()
        assert [s["session_id"] for s in sessions][:2] == [second["session_id"], first["session_id"]]
        detail = (await client.get(f"/api/sessions/{first['session_id']}?events=true")).json()
        assert detail["runs"][0]["events"][0]["event_type"] == "task_start"
        assert (await client.delete("/api/memory")).json() == {"cleared": True}
        assert (await client.get("/api/memory")).json() == []

    # a run left "running" by a stopped server is closed on the next start
    ws.telemetry.start_run(run_id="run-orphan", task_id="manual", label="manual", category="manual",
                           model="m", status="running")
    assert ws.telemetry.mark_interrupted() == 1
    assert ws.telemetry.get_run("run-orphan")["status"] == "interrupted"


async def test_chat_memory_is_separate_from_experiments(ws):
    from aiworkspace.tasks import task_turns

    # a chat saves a note: the scripted model replays the remember step for this prompt
    prompt = task_turns(ws.tasks["preference_across_sessions"])[0]["prompt"]
    await ws.run(prompt=prompt, model="test/scripted")
    assert len(ws.chat_memory.all()) == 1 and ws.memory.all() == []
    # task runs use the experiments memory, cleared before each run, and never touch the chat's
    await ws.run(task_id="remember_deadline", model="test/scripted")
    assert len(ws.memory.all()) == 1 and len(ws.chat_memory.all()) == 1
    await ws.run(task_id="growth_calculation", model="test/scripted")
    assert ws.memory.all() == [], "task runs start from empty memory"
    assert len(ws.chat_memory.all()) == 1, "the chat's note survives task runs"
    # a new chat recalls it
    await ws.run(prompt="What do you remember about my summaries?", model="test/scripted")
    recalled = [e for e in ws.telemetry.events() if e["event_type"] == "memory_read" and e["label"] == "manual"]
    assert recalled and "three bullet" in recalled[-1]["content"]
