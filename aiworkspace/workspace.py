"""Workspace orchestrator: wires sandbox, gateway, memory, models, hooks and telemetry together and
runs tasks (single runs, repeated runs and batches) reproducibly.
"""
from __future__ import annotations

import asyncio
import json
import uuid

from .agent import Agent
from .config import config_hash, deep_merge, resolve_path, workspace_config
from .context import RunContext
from .gateway import ToolGateway
from .hooks import HookManager, build_manager
from .memory import LongTermMemory
from .models import ModelClient, resolve_model
from .sandbox.client import make_sandbox
from .tasks import evaluate, load_tasks
from .telemetry import Telemetry, now_iso


class Workspace:
    def __init__(self, cfg: dict | None = None, hooks: HookManager | None = None, sandbox=None,
                 telemetry: Telemetry | None = None, memory: LongTermMemory | None = None, model_client=None):
        self.cfg = cfg or workspace_config()
        self.hooks = hooks or build_manager(self.cfg)
        tcfg = self.cfg.get("telemetry", {})
        self.telemetry = telemetry or Telemetry(resolve_path(tcfg.get("db_path", "runtime/telemetry.db")),
                                                tcfg.get("content_max_chars", 4000))
        self.memory = memory or LongTermMemory(resolve_path(self.cfg.get("memory", {}).get("db_path", "runtime/memory.db")))
        self.sandbox = sandbox or make_sandbox(self.cfg)
        self.gateway = ToolGateway(self.sandbox, self.memory)
        self.models = model_client or ModelClient(self.cfg)
        self.agent = Agent(self.models, self.gateway, self.memory, self.cfg)
        self._lock = asyncio.Lock()   # one run at a time: the sandbox is shared state (NFR-3)
        self.tasks: dict[str, dict] = {}

    async def start(self) -> None:
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
                  repeat_index: int = 0, batch_id: str | None = None, run_id: str | None = None) -> dict:
        if task_id:
            if task_id not in self.tasks:
                raise KeyError(f"unknown task {task_id!r}")
            task = self.tasks[task_id]
        elif prompt:
            task = {"id": "manual", "name": "Manual prompt", "label": "manual", "category": "manual", "prompt": prompt}
        else:
            raise ValueError("give a task_id or a prompt")
        model = model or task.get("model") or self.cfg["agent"]["default_model"]
        run_id = run_id or f"run-{uuid.uuid4().hex[:12]}"
        agent_id = self.cfg["agent"].get("agent_id", "workspace-agent-1")
        self.telemetry.start_run(
            run_id=run_id, task_id=task["id"], label=task["label"], category=task["category"], model=model,
            defence=self.hooks.defence_label(),
            agent_id=agent_id, repeat_index=repeat_index, batch_id=batch_id, prompt=task["prompt"], status="queued",
            config_hash=config_hash(deep_merge(self.cfg, {"model": resolve_model(model)["model"], "task": task.get("_file")})),
            seed=self.cfg["agent"].get("seed"), hooks_json=json.dumps(self.hooks.status()),
        )
        async with self._lock:
            self.telemetry.update_run(run_id, status="running", started_at=now_iso())
            ctx = RunContext(
                run_id=run_id, task_id=task["id"], agent_id=agent_id, model=model, telemetry=self.telemetry,
                hooks=self.hooks, trust_levels=(self.cfg.get("provenance") or {}).get("trust_levels", {}),
                label=task["label"], category=task["category"],
                task={k: task.get(k) for k in ("id", "name", "label", "category", "prompt", "authorised", "description")},
            )
            try:
                await self._prepare(task, ctx)
                result = await self.agent.run(task["prompt"], ctx, allowed_tools=task.get("tools"))
                events = self.telemetry.events(run_id)
                evaluation = await evaluate(task, result["output"], events, self.sandbox)
                ctx.log("task_end", outcome=_outcome(result["status"], evaluation),
                        details={"status": result["status"], "task_success": evaluation["task_success"],
                                 "attack_success": evaluation["attack_success"]})
                self.telemetry.update_run(
                    run_id, status=result["status"], ended_at=now_iso(), steps=result["steps"],
                    final_output=result["output"], task_success=evaluation["task_success"],
                    attack_success=evaluation["attack_success"], evaluation_json=evaluation,
                )
            except Exception as exc:
                ctx.log("error", outcome="run_error", content=f"{type(exc).__name__}: {exc}")
                self.telemetry.update_run(run_id, status="error", ended_at=now_iso(), error=f"{type(exc).__name__}: {exc}")
                raise
        return self.telemetry.get_run(run_id)

    async def _prepare(self, task: dict, ctx: RunContext) -> None:
        """Clean sandbox + memory (FR-13), then apply the task's setup."""
        setup = task.get("setup") or {}
        if (self.cfg.get("sandbox") or {}).get("reset_between_runs", True):
            await self.sandbox.reset(setup.get("files"))
        if (self.cfg.get("memory") or {}).get("reset_between_runs", True):
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
