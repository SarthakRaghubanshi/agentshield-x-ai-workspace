"""HTTP front for the sandbox container (docker compose service `sandbox`).

Only the workspace container can reach it (internal Docker network, no internet).
    uvicorn aiworkspace.sandbox.server:app --host 0.0.0.0 --port 8100
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from pydantic import BaseModel

from .runtime import SandboxRuntime

runtime = SandboxRuntime()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await runtime.start()
    yield
    await runtime.stop()


app = FastAPI(title="AI Workspace sandbox", lifespan=lifespan)


class ExecuteRequest(BaseModel):
    name: str
    args: dict = {}


class ResetRequest(BaseModel):
    overlay: dict[str, str] | None = None


@app.get("/health")
async def health():
    return {"ok": True, "resets": runtime.reset_count, "mcp_errors": runtime.mcp.errors if runtime.mcp else {}}


@app.get("/tools")
async def tools():
    return runtime.list_tools()


@app.post("/execute")
async def execute(req: ExecuteRequest):
    return await runtime.execute(req.name, req.args)


@app.post("/reset")
async def reset(req: ResetRequest):
    return await runtime.reset(req.overlay)


@app.get("/state")
async def state():
    return runtime.state()


@app.get("/file")
async def file(path: str):
    return {"path": path, "content": runtime.read_sandbox_file(path)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8100)
