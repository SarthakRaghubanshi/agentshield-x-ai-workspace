"""The sandbox runtime: owns the sandbox directory, the native tools, RAG and MCP servers.

In Docker it runs in its own container (no internet, read-only image, non-root, dropped
capabilities) behind `aiworkspace.sandbox.server`. For local development it runs in-process.
Either way the agent only reaches it through the `execute_tool()` gateway.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import time
from pathlib import Path

from ..config import ROOT, mcp_config
from .mcp_bridge import MCPBridge
from .rag import KnowledgeBase
from .tools import NativeTools, ToolError, ToolSpec

log = logging.getLogger("aiworkspace.sandbox")

SEED_DIR = Path(os.environ.get("SANDBOX_SEED", ROOT / "sandbox_seed"))
DEFAULT_ROOT = Path(os.environ.get("SANDBOX_ROOT", ROOT / "runtime" / "sandbox"))


class SandboxRuntime:
    def __init__(self, root: Path | None = None, seed: Path | None = None, mcp_config_path: str | None = None, enable_mcp: bool = True):
        self.root = Path(root or DEFAULT_ROOT)
        self.seed = Path(seed or SEED_DIR)
        self.mcp_config_path = mcp_config_path
        self.enable_mcp = enable_mcp
        self.mcp: MCPBridge | None = None
        self.tools: NativeTools | None = None
        self.reset_count = 0
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        self._reset_files()
        if self.enable_mcp:
            servers = mcp_config(self.mcp_config_path).get("servers", {})
            self.mcp = MCPBridge(servers, self.root)
            await self.mcp.start()

    async def stop(self) -> None:
        if self.mcp:
            await self.mcp.stop()

    # ---- reset (FR-13) ------------------------------------------------------
    def _reset_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        for child in self.root.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in self.seed.iterdir():
            dest = self.root / child.name
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)
        db_dir = self.root / "db"
        seed_sql = db_dir / "seed.sql"
        if seed_sql.exists():
            conn = sqlite3.connect(db_dir / "customers.db")
            conn.executescript(seed_sql.read_text(encoding="utf-8"))
            conn.commit()
            conn.close()
            seed_sql.unlink()
        (self.root / "outbox").mkdir(exist_ok=True)
        self.tools = NativeTools(self.root, rag=KnowledgeBase(self.root / "kb"))
        self.reset_count += 1

    async def reset(self, overlay: dict | None = None) -> dict:
        """Restore the pristine seed, then apply an optional per-task overlay {relative_path: text}."""
        async with self._lock:
            self._reset_files()
            for rel, content in (overlay or {}).items():
                target = (self.root / rel).resolve()
                if not str(target).startswith(str(self.root.resolve())):
                    raise ValueError(f"overlay path escapes sandbox: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            if overlay and any(k.startswith("kb/") for k in overlay):
                self.tools.rag.build()
        return {"reset": True, "count": self.reset_count, "root": str(self.root)}

    # ---- tools ----------------------------------------------------------------
    def specs(self) -> dict[str, ToolSpec]:
        specs = dict(self.tools.specs)
        if self.mcp:
            specs.update(self.mcp.specs)
        return specs

    def list_tools(self) -> list[dict]:
        return [s.metadata() for s in self.specs().values()]

    async def execute(self, name: str, args: dict) -> dict:
        """Run one tool. Returns {"ok", "output", "error", "duration_ms"}; never raises for tool errors."""
        start = time.perf_counter()
        try:
            if self.mcp and name in self.mcp.specs:
                output = await self.mcp.call(name, args)
            elif name in self.tools.impls:
                output = await asyncio.to_thread(self.tools.impls[name], **(args or {}))
            else:
                raise ToolError(f"unknown tool {name!r}")
            result = {"ok": True, "output": output, "error": None}
        except TypeError as exc:
            result = {"ok": False, "output": f"ERROR: bad arguments for {name}: {exc}", "error": str(exc)}
        except ToolError as exc:
            result = {"ok": False, "output": f"ERROR: {exc}", "error": str(exc)}
        except Exception as exc:
            log.exception("tool %s crashed", name)
            result = {"ok": False, "output": f"ERROR: tool {name} failed: {exc}", "error": str(exc)}
        result["duration_ms"] = round((time.perf_counter() - start) * 1000, 2)
        return result

    # ---- state inspection (used by the task evaluator, not exposed to the agent) -----
    def state(self) -> dict:
        def jsonl(path: Path) -> list:
            if not path.exists():
                return []
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

        files_dir = self.root / "files"
        return {
            "emails_sent": jsonl(self.root / "outbox" / "emails.jsonl"),
            "messages_sent": jsonl(self.root / "outbox" / "messages.jsonl"),
            "files": sorted(p.relative_to(files_dir).as_posix() for p in files_dir.rglob("*") if p.is_file()),
            "calendar": json.loads((self.root / "calendar" / "events.json").read_text(encoding="utf-8"))
            if (self.root / "calendar" / "events.json").exists() else [],
        }

    def read_sandbox_file(self, rel: str) -> str | None:
        target = (self.root / "files" / rel).resolve()
        if not str(target).startswith(str((self.root / "files").resolve())) or not target.is_file():
            return None
        return target.read_text(encoding="utf-8", errors="replace")
