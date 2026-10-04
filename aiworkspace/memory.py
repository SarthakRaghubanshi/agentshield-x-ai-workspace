"""Short-term and long-term memory (PRD FR-14, FR-16).

* Short-term memory is the run's conversation (the message list held by the agent loop).
* Long-term memory is a persistent SQLite store. Every write goes through ONE function,
  `LongTermMemory.write_memory()`, which carries source + trust metadata and passes the
  `before_memory_write` extension point.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from pathlib import Path

from .context import RunContext
from .hooks import before_memory_write
from .telemetry import now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, source TEXT NOT NULL,
    trust_level TEXT, run_id TEXT, task_id TEXT, created_at TEXT, metadata_json TEXT
);
"""


class LongTermMemory:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)

    def reset(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM memories")
            self._conn.commit()

    def all(self) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM memories ORDER BY id")]

    def search(self, query: str, k: int = 3) -> list[dict]:
        """Keyword-overlap retrieval; returns the most recent memories when nothing overlaps."""
        terms = {t for t in re.findall(r"\w+", query.lower()) if len(t) > 2}
        rows = self.all()
        scored = sorted(
            rows,
            key=lambda r: (-len(terms & set(re.findall(r"\w+", r["content"].lower()))), -r["id"]),
        )
        return scored[:k]

    async def write_memory(self, content: str, *, source: str, ctx: RunContext,
                           trust_level: str | None = None, metadata: dict | None = None) -> dict:
        """The single entry point for every long-term memory write (FR-16)."""
        trust_level = trust_level or ctx.trust(source)
        payload = {"content": content, "source": source, "trust_level": trust_level, "metadata": metadata or {}}
        outcome = await before_memory_write(payload, ctx.hook_ctx(source, extra={"memory": payload}), ctx.hooks)
        payload = outcome.payload if isinstance(outcome.payload, dict) else payload
        if not outcome.allowed:
            ctx.log("memory_write", hook=outcome, resource_accessed="long_term_memory", provenance_source=source,
                    trust_level=trust_level, outcome="blocked", content=content, details={"stored": False})
            return {"stored": False, "decision": outcome.decision.value, "reason": outcome.reason}
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO memories (content, source, trust_level, run_id, task_id, created_at, metadata_json) VALUES (?,?,?,?,?,?,?)",
                [payload["content"], payload["source"], payload["trust_level"], ctx.run_id, ctx.task_id, now_iso(),
                 json.dumps(payload.get("metadata") or {})],
            )
            self._conn.commit()
        ctx.log("memory_write", hook=outcome, resource_accessed="long_term_memory", provenance_source=payload["source"],
                trust_level=payload["trust_level"], outcome="stored", content=payload["content"],
                details={"stored": True, "memory_id": cur.lastrowid})
        return {"stored": True, "memory_id": cur.lastrowid}
