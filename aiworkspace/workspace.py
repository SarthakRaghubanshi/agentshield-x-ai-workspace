"""Workspace orchestrator: wires sandbox, gateway, memory, models, hooks and telemetry together and
runs tasks (single runs, repeated runs and batches) reproducibly.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from .agent import Agent
from .config import ROOT, config_hash, deep_merge, resolve_path, workspace_config
from .context import RunContext
from .gateway import ToolGateway
from .hooks import HookManager, build_manager
from .memory import LongTermMemory
from .models import ModelClient, resolve_model
from .sandbox.client import make_sandbox
from .tasks import evaluate, load_tasks, task_turns
from .telemetry import Telemetry, now_iso


class Workspace:
    def __init__(self, cfg: dict | None = None, hooks: HookManager | None = None, sandbox=None,
                 telemetry: Telemetry | None = None, memory: LongTermMemory | None = None, model_client=None):
        self.cfg = cfg or workspace_config()
        self.hooks = hooks or build_manager(self.cfg)
        tcfg = self.cfg.get("telemetry", {})
        self.telemetry = telemetry or Telemetry(resolve_path(tcfg.get("db_path", "runtime/telemetry.db")),
                                                tcfg.get("content_max_chars", 4000), db_url=tcfg.get("db_url"))
        self.memory = memory or LongTermMemory(resolve_path(self.cfg.get("memory", {}).get("db_path", "runtime/memory.db")))
        self.sandbox = sandbox or make_sandbox(self.cfg)
        self.gateway = ToolGateway(self.sandbox, self.memory)
        self.models = model_client or ModelClient(self.cfg)
        self.agent = Agent(self.models, self.gateway, self.memory, self.cfg)
        self._lock = asyncio.Lock()   # one run at a time: the sandbox is shared state (NFR-3)
        self.tasks: dict[str, dict] = {}
        self._sandbox_session: str | None = None   # which conversation the sandbox state belongs to

    async def start(self) -> None:
        # Runs left "running" by a previous process that was stopped mid-run.
        self.telemetry.mark_interrupted()
        await self.sandbox.start()
        await self.gateway.refresh()
        self.reload_tasks()

    async def stop(self) -> None:
        await self.sandbox.stop()

    def reload_tasks(self) -> dict[str, dict]:
        self.tasks = load_tasks(self.cfg.get("tasks_dir", "tasks"))
        return self.tasks

    # ---- running ----------------------------------------------------------------
    async def run(self, *, task_id: str | None = None, prompt: str | None = None, model: str | None = None,
                  repeat_index: int = 0, batch_id: str | None = None, run_id: str | None = None,
                  session_id: str | None = None, keep_memory: bool = False) -> dict:
        """Run a task (all its turns) or a typed prompt.

        `session_id` continues an earlier conversation (a console follow-up): its short-term
        memory is restored and the sandbox / long-term memory are NOT reset, so the agent sees the
        state the previous turn left behind. `keep_memory` starts a new conversation on a clean
        sandbox but keeps long-term memory (the chat screen uses this).
        """
        if task_id:
            if task_id not in self.tasks:
                raise KeyError(f"unknown task {task_id!r}")
            task = self.tasks[task_id]
        elif prompt:
            task = {"id": "manual", "name": "Manual prompt", "label": "manual", "category": "manual", "prompt": prompt}
        else:
            raise ValueError("give a task_id or a prompt")
        turns = task_turns(task) if task_id else [{"prompt": prompt, "new_conversation": False}]
        continuing = bool(session_id) and self.memory.conversations.exists(session_id)
        session_id = session_id or f"sess-{uuid.uuid4().hex[:10]}"
        model = model or task.get("model") or self.cfg["agent"]["default_model"]
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        agent_id = self.cfg["agent"].get("agent_id", "workspace-agent-1")
        self.telemetry.start_run(
            run_id=run_id, task_id=task["id"], label=task["label"], category=task["category"], model=model,
            defence=self.hooks.defence_label(), session_id=session_id, turns=len(turns),
            agent_id=agent_id, repeat_index=repeat_index, batch_id=batch_id,
            prompt="\n\n".join(t["prompt"] for t in turns), status="queued",
            config_hash=config_hash(deep_merge(self.cfg, {"model": resolve_model(model)["model"], "task": task.get("_file")})),
            seed=self.cfg["agent"].get("seed"), hooks_json=json.dumps(self.hooks.status()),
        )
        async with self._lock:
            self.telemetry.update_run(run_id, status="running", started_at=now_iso())
            ctx = RunContext(
                run_id=run_id, task_id=task["id"], agent_id=agent_id, model=model, telemetry=self.telemetry,
                hooks=self.hooks, trust_levels=(self.cfg.get("provenance") or {}).get("trust_levels", {}),
                label=task["label"], category=task["category"], session_id=session_id,
                task={k: task.get(k) for k in ("id", "name", "label", "category", "prompt", "authorised", "description")},
            )
            try:
                ctx.log("task_start", provenance_source="user", trust_level=ctx.trust("user"), content=task["prompt"],
                        details={"turns": len(turns), "session_id": session_id, "continues_session": continuing,
                                 "sandbox_continuity": (self._sandbox_session == session_id) if continuing else None,
                                 "max_steps_per_turn": self.agent.max_steps, "allowed_tools": task.get("tools")})
                if continuing:
                    history = self.memory.conversations.get(session_id)
                else:
                    history = []
                    await self._prepare(task, ctx, keep_memory=keep_memory)
                outputs, status, steps = [], "completed", 0
                for i, turn in enumerate(turns, start=1):
                    ctx.turn = i
                    if turn["new_conversation"]:
                        history = []  # fresh short-term memory; long-term memory and sandbox persist
                    result = await self.agent.run(turn["prompt"], ctx, allowed_tools=task.get("tools"), history=history)
                    history = result["messages"]
                    outputs.append(result["output"])
                    steps += result["steps"]
                    status = result["status"]
                    if status == "error":
                        break
                self.memory.conversations.save(session_id, history, model)
                self._sandbox_session = session_id
                if len(outputs) == 1:
                    final_output = outputs[0]
                else:
                    final_output = "\n\n".join(f"[turn {i}] {o}" for i, o in enumerate(outputs, start=1))
                events = self.telemetry.events(run_id)
                evaluation = await evaluate(task, "\n\n".join(outputs), events, self.sandbox)
                ctx.log("task_end", outcome=_outcome(status, evaluation),
                        details={"status": status, "task_success": evaluation["task_success"],
                                 "attack_success": evaluation["attack_success"]})
                self.telemetry.update_run(
                    run_id, status=status, ended_at=now_iso(), steps=steps, final_output=final_output,
                    task_success=evaluation["task_success"], attack_success=evaluation["attack_success"],
                    evaluation_json=evaluation,
                )
            except Exception as exc:
                ctx.log("error", outcome="run_error", content=f"{type(exc).__name__}: {exc}")
                self.telemetry.update_run(run_id, status="error", ended_at=now_iso(), error=f"{type(exc).__name__}: {exc}")
                raise
        return self.telemetry.get_run(run_id)

    async def _prepare(self, task: dict, ctx: RunContext, keep_memory: bool = False) -> None:
        """Clean sandbox + memory (FR-13), then apply the task's setup."""
        setup = task.get("setup") or {}
        overlay = dict(setup.get("files") or {})
        for dest, src in (setup.get("copy") or {}).items():
            # Task-specific content kept outside the shared seed (e.g. poisoned documents), so
            # benign tasks always see a clean sandbox.
            overlay[dest] = (ROOT / "attack_assets" / src).read_text(encoding="utf-8")
        if (self.cfg.get("sandbox") or {}).get("reset_between_runs", True):
            await self.sandbox.reset(overlay)
        if not keep_memory and (self.cfg.get("memory") or {}).get("reset_between_runs", True):
            self.memory.reset()
        for item in setup.get("memory", []) or []:
            await self.memory.write_memory(item["content"], source=item.get("source", "memory"), ctx=ctx,
                                           trust_level=item.get("trust_level"), metadata={"seeded_by_task": True})

    async def run_batch(self, task_ids: list[str], model: str | None = None, repeats: int | None = None,
                        batch_id: str | None = None) -> dict:
        """Run each task `repeats` times (FR-27). Runs are sequential for reproducibility."""
        batch_id = batch_id or f"batch-{uuid.uuid4().hex[:8]}"
        runs = []
        for task_id in task_ids:
            n = repeats or self.tasks[task_id].get("repeats", 1)
            for i in range(n):
                try:
                    runs.append(await self.run(task_id=task_id, model=model, repeat_index=i, batch_id=batch_id))
                except Exception as exc:  # keep going; the error is recorded on the run
                    runs.append({"task_id": task_id, "repeat_index": i, "status": "error", "error": str(exc)})
        return {"batch_id": batch_id, "runs": runs, "summary": self.telemetry.summary(batch_id)}


def _outcome(status: str, evaluation: dict) -> str:
    if status != "completed":
        return status
    if evaluation.get("task_success") is None:
        return "completed"
    return "task_success" if evaluation["task_success"] else "task_failure"
