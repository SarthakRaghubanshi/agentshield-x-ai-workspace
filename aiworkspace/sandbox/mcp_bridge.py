"""MCP support (PRD FR-8): connect configured MCP servers over stdio and expose their tools.

Servers are listed in config/mcp_servers.yaml. Their tools appear in the tool gateway as
`mcp__<server>__<tool>` with the FR-10 metadata given in the config - no code changes needed.
"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import Client, StdioServerParameters

from ..config import ROOT
from .tools import ToolError, ToolSpec

log = logging.getLogger("aiworkspace.mcp")


class MCPBridge:
    def __init__(self, servers: dict, sandbox_root: Path):
        self.servers_cfg = {k: v for k, v in (servers or {}).items() if v.get("enabled", True)}
        self.sandbox_root = Path(sandbox_root)
        self.clients: dict[str, Client] = {}
        self.specs: dict[str, ToolSpec] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._stack: AsyncExitStack | None = None
        self.errors: dict[str, str] = {}

    async def start(self) -> None:
        self._stack = AsyncExitStack()
        for name, cfg in self.servers_cfg.items():
            if cfg.get("url"):
                # Remote server over Streamable HTTP, e.g. http://notes-mcp:9000/mcp
                target = cfg["url"]
            else:
                # Local server launched as a subprocess and spoken to over stdio.
                command = cfg["command"]
                if command in ("python", "python3"):
                    command = sys.executable  # use the workspace interpreter
                env = dict(os.environ) | {k: str(v) for k, v in (cfg.get("env") or {}).items()}
                env["SANDBOX_ROOT"] = str(self.sandbox_root)
                target = StdioServerParameters(command=command, args=list(cfg.get("args", [])), env=env, cwd=str(ROOT))
            try:
                client = await self._stack.enter_async_context(Client(target, read_timeout_seconds=30))
                listing = await client.list_tools()
            except Exception as exc:
                log.exception("MCP server %s failed to start", name)
                self.errors[name] = str(exc)
                continue
            self.clients[name] = client
            for tool in listing.tools:
                full = f"mcp__{name}__{tool.name}"
                self._routes[full] = (name, tool.name)
                self.specs[full] = ToolSpec(
                    name=full,
                    description=f"[MCP:{name}] {tool.description or tool.name}",
                    parameters=tool.input_schema or {"type": "object", "properties": {}},
                    risk_level=cfg.get("risk_level", "medium"),
                    resources=list(cfg.get("resources", [f"mcp:{name}"])),
                    scope=cfg.get("scope", f"mcp:{name}"),
                    output_source=cfg.get("output_source", "tool_output"),
                    resource_template=f"mcp:{name}/{tool.name}",
                    location="mcp",
                    extra={"server": name},
                )
            log.info("MCP server %s connected with %d tools", name, len(listing.tools))

    async def call(self, full_name: str, args: dict) -> str:
        server, tool = self._routes[full_name]
        result = await self.clients[server].call_tool(tool, args or {})
        parts = [getattr(c, "text", None) or str(c) for c in (getattr(result, "content", None) or [])]
        text = "\n".join(parts)
        if getattr(result, "is_error", False):
            raise ToolError(text or "MCP tool error")
        return text

    async def stop(self) -> None:
        if self._stack:
            try:
                await self._stack.aclose()
            except Exception:  # pragma: no cover - shutdown noise from subprocess teardown
                log.debug("MCP shutdown error", exc_info=True)
            self._stack = None
            self.clients.clear()
