"""FastAPI backend + task console UI (PRD FR-28..FR-30).

    uvicorn aiworkspace.api:app --port 8000      then open http://localhost:8000
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__
from .hooks import HOOK_POINTS, set_manager
from .models import list_models
from .tasks import public_task
from .workspace import Workspace

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
UI_DIR = Path(__file__).parent / "ui"

ws: Workspace | None = None
_background: set[asyncio.Task] = set()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global ws
    ws = Workspace()
    set_manager(ws.hooks)
    await ws.start()
    yield
    await ws.stop()


app = FastAPI(title="AgentShield-X AI Workspace", version=__version__, lifespan=lifespan)


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:  # already recorded on the run
        logging.getLogger("aiworkspace.api").exception("run failed")


# ---- UI ---------------------------------------------------------------------------
@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(UI_DIR / "index.html")


# ---- metadata -----------------------------------------------------------------------
@app.get("/api/health")
async def health():
    return {"ok": True, "version": __version__, "sandbox_mode": ws.sandbox.mode, "tools": len(ws.gateway.specs)}


@app.get("/api/models")
async def models():
    return {"default": ws.cfg["agent"]["default_model"], "models": await list_models()}


@app.get("/api/tasks")
async def tasks():
    ws.reload_tasks()
    return [public_task(t) for t in ws.tasks.values()]


@app.get("/api/tools")
async def tools():
    return ws.gateway.metadata()


@app.get("/api/hooks")
async def hooks():
    return ws.hooks.status()


class HookToggle(BaseModel):
    point: str
    enabled: bool


@app.post("/api/hooks")
async def toggle_hook(req: HookToggle):
    if req.point not in HOOK_POINTS:
        raise HTTPException(400, f"unknown extension point {req.point}")
    ws.hooks.set_enabled(req.point, req.enabled)
    return ws.hooks.status()


# ---- runs -------------------------------------------------------------------------------
class RunRequest(BaseModel):
    task_id: str | None = None
    prompt: str | None = None
    model: str | None = None
    repeats: int = 1


@app.post("/api/runs")
async def start_run(req: RunRequest):
    if not req.task_id and not (req.prompt or "").strip():
        raise HTTPException(400, "give a task_id or a prompt")
    if req.task_id and req.task_id not in ws.tasks:
        raise HTTPException(404, f"unknown task {req.task_id}")
    repeats = max(1, min(req.repeats, 50))
    batch_id = f"batch-{uuid.uuid4().hex[:8]}" if repeats > 1 else None
    run_ids = [f"run-{uuid.uuid4().hex[:12]}" for _ in range(repeats)]

    async def go():
        for i, run_id in enumerate(run_ids):
            await _safe(ws.run(task_id=req.task_id, prompt=req.prompt, model=req.model,
                               repeat_index=i, batch_id=batch_id, run_id=run_id))

    _spawn(go())
    return {"run_ids": run_ids, "batch_id": batch_id}


class BatchRequest(BaseModel):
    task_ids: list[str] | None = None   # None = all tasks
    label: str | None = None            # optional filter: benign | attack
    model: str | None = None
    repeats: int = 1


@app.post("/api/batches")
async def start_batch(req: BatchRequest):
    ids = req.task_ids or [t["id"] for t in ws.tasks.values() if not req.label or t["label"] == req.label]
    unknown = [i for i in ids if i not in ws.tasks]
    if unknown:
        raise HTTPException(404, f"unknown tasks {unknown}")
    batch_id = f"batch-{uuid.uuid4().hex[:8]}"
    _spawn(_safe(ws.run_batch(ids, model=req.model, repeats=max(1, min(req.repeats, 50)), batch_id=batch_id)))
    return {"batch_id": batch_id, "tasks": ids}


@app.get("/api/runs")
async def runs(limit: int = 200, batch_id: str | None = None):
    return ws.telemetry.list_runs(limit, batch_id)


@app.get("/api/runs/{run_id}")
async def run_detail(run_id: str):
    run = ws.telemetry.get_run(run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return {"run": run, "events": ws.telemetry.events(run_id)}


@app.get("/api/runs/{run_id}/stream")
async def run_stream(run_id: str):
    """Server-sent events: every telemetry event of the run as it happens (FR-29)."""
    queue = ws.telemetry.subscribe(run_id)

    async def gen():
        try:
            run = ws.telemetry.get_run(run_id)
            last_id = 0
            for event in ws.telemetry.events(run_id):  # catch up on anything already logged
                last_id = event["event_id"]
                yield f"data: {json.dumps({'kind': 'event', 'event': event}, default=str)}\n\n"
            if run and run["status"] not in ("queued", "running"):
                yield f"data: {json.dumps({'kind': 'done', 'run': run}, default=str)}\n\n"
                return
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if msg["kind"] == "event":
                    if msg["event"]["event_id"] <= last_id:
                        continue
                    last_id = msg["event"]["event_id"]
                yield f"data: {json.dumps(msg, default=str)}\n\n"
                if msg["kind"] == "done":
                    return
        finally:
            ws.telemetry.unsubscribe(run_id, queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/summary")
async def summary(batch_id: str | None = None):
    return ws.telemetry.summary(batch_id)


# ---- export (FR-23) ----------------------------------------------------------------------
@app.get("/api/export")
async def export(fmt: str = Query("csv", pattern="^(csv|json)$"), table: str = Query("events", pattern="^(events|runs)$"),
                 run_id: str | None = None):
    body = ws.telemetry.export(fmt, table, run_id)
    name = f"{table}{'-' + run_id if run_id else ''}.{fmt}"
    media = "application/json" if fmt == "json" else "text/csv"
    return Response(body, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


# ---- sandbox ----------------------------------------------------------------------------------
@app.post("/api/sandbox/reset")
async def sandbox_reset():
    async with ws._lock:
        result = await ws.sandbox.reset()
        ws.memory.reset()
    return result


@app.get("/api/sandbox/state")
async def sandbox_state():
    return await ws.sandbox.state()


@app.get("/api/memory")
async def memory():
    return ws.memory.all()


@app.get("/api/schema", response_class=PlainTextResponse)
async def schema():
    from .telemetry import EVENT_COLUMNS

    return "\n".join(EVENT_COLUMNS)
