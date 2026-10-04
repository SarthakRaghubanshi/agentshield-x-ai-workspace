"""How the agent process talks to the sandbox: in-process (local dev) or over HTTP (Docker)."""
from __future__ import annotations

import httpx

from .runtime import SandboxRuntime


class LocalSandbox:
    """Sandbox runtime in the same process. Used for local development and tests."""

    mode = "local"

    def __init__(self, runtime: SandboxRuntime | None = None, **kwargs):
        self.runtime = runtime or SandboxRuntime(**kwargs)

    async def start(self) -> None:
        await self.runtime.start()

    async def stop(self) -> None:
        await self.runtime.stop()

    async def list_tools(self) -> list[dict]:
        return self.runtime.list_tools()

    async def execute(self, name: str, args: dict) -> dict:
        return await self.runtime.execute(name, args)

    async def reset(self, overlay: dict | None = None) -> dict:
        return await self.runtime.reset(overlay)

    async def state(self) -> dict:
        return self.runtime.state()

    async def read_file(self, path: str) -> str | None:
        return self.runtime.read_sandbox_file(path)


class RemoteSandbox:
    """Sandbox running in its own container, reached over the internal Docker network."""

    mode = "remote"

    def __init__(self, url: str, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.url, timeout=timeout)

    async def start(self) -> None:
        (await self.http.get("/health")).raise_for_status()

    async def stop(self) -> None:
        await self.http.aclose()

    async def list_tools(self) -> list[dict]:
        r = await self.http.get("/tools")
        r.raise_for_status()
        return r.json()

    async def execute(self, name: str, args: dict) -> dict:
        r = await self.http.post("/execute", json={"name": name, "args": args})
        r.raise_for_status()
        return r.json()

    async def reset(self, overlay: dict | None = None) -> dict:
        r = await self.http.post("/reset", json={"overlay": overlay})
        r.raise_for_status()
        return r.json()

    async def state(self) -> dict:
        r = await self.http.get("/state")
        r.raise_for_status()
        return r.json()

    async def read_file(self, path: str) -> str | None:
        r = await self.http.get("/file", params={"path": path})
        r.raise_for_status()
        return r.json()["content"]


def make_sandbox(cfg: dict):
    scfg = cfg.get("sandbox", {}) or {}
    if scfg.get("mode", "local") == "remote":
        return RemoteSandbox(scfg.get("url", "http://sandbox:8100"))
    return LocalSandbox(mcp_config_path=scfg.get("mcp_config"))
