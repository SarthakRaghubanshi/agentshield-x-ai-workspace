"""Telemetry logger (PRD FR-21..FR-23, SRS Ch. 9.1).

Every observable agent action is one row in `events`. The SRS columns come first; security
columns (guard results, scores, decision) exist but stay NULL until the security layer fills
them through the extension points. Extra columns (model, tokens, latency, args...) are there for
feature engineering by the ML teammates.

Storage: SQLite by default (`telemetry.db_path`). For larger experiment volumes set
`telemetry.db_url` (or the TELEMETRY_DB_URL environment variable) to a PostgreSQL URL,
e.g. postgresql://agentshield:secret@localhost:5432/telemetry  (PRD FR-21).
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# SRS Ch. 9.1 fields, in order (tool_called/resource_accessed and the paired fields are split).
SRS_COLUMNS = [
    "timestamp", "task_id", "agent_id", "event_type", "tool_called", "resource_accessed",
    "input_guard_result", "output_guard_result", "authorisation_result",
    "provenance_source", "trust_level", "anomaly_score", "risk_score", "final_decision", "outcome",
]
EXTRA_COLUMNS = [
    "run_id", "step", "turn", "label", "category", "model", "latency_ms", "prompt_tokens",
    "completion_tokens", "total_tokens", "cost_usd", "args_json", "content", "details_json",
]
EVENT_COLUMNS = ["event_id"] + SRS_COLUMNS + EXTRA_COLUMNS

EVENT_TYPES = {
    "task_start", "input_received", "memory_read", "model_call", "tool_call", "tool_result",
    "memory_write", "output_generated", "task_end", "error",
}

RUN_COLUMNS = [
    "run_id", "task_id", "label", "category", "model", "defence", "agent_id", "repeat_index", "batch_id",
    "session_id", "turns",
    "started_at", "ended_at", "status", "steps", "prompt", "final_output", "task_success",
    "attack_success", "evaluation_json", "config_hash", "seed", "hooks_json", "error",
]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id {autoinc},
    timestamp TEXT NOT NULL, task_id TEXT, agent_id TEXT, event_type TEXT NOT NULL,
    tool_called TEXT, resource_accessed TEXT,
    input_guard_result TEXT, output_guard_result TEXT, authorisation_result TEXT,
    provenance_source TEXT, trust_level TEXT, anomaly_score REAL, risk_score REAL,
    final_decision TEXT, outcome TEXT,
    run_id TEXT, step INTEGER, turn INTEGER, label TEXT, category TEXT, model TEXT, latency_ms REAL,
    prompt_tokens INTEGER, completion_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
    args_json TEXT, content TEXT, details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, task_id TEXT, label TEXT, category TEXT, model TEXT, defence TEXT, agent_id TEXT,
    repeat_index INTEGER, batch_id TEXT, session_id TEXT, turns INTEGER, started_at TEXT, ended_at TEXT, status TEXT, steps INTEGER,
    prompt TEXT, final_output TEXT, task_success INTEGER, attack_success INTEGER,
    evaluation_json TEXT, config_hash TEXT, seed INTEGER, hooks_json TEXT, error TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class _SQLite:
    """SQLite backend (default)."""

    placeholder = "?"

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA.format(autoinc="INTEGER PRIMARY KEY AUTOINCREMENT"))
        self.conn.commit()

    def columns(self, table: str) -> set[str]:
        return {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def execute(self, sql: str, params: list) -> None:
        self.conn.execute(sql, params)
        self.conn.commit()

    def insert_returning_id(self, sql: str, params: list, id_column: str) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    def query(self, sql: str, params: list) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def close(self) -> None:
        self.conn.close()


class _Postgres:
    """PostgreSQL backend (optional; needs `psycopg`)."""

    placeholder = "%s"

    def __init__(self, url: str):
        import psycopg
        from psycopg.rows import dict_row

        for attempt in range(15):  # the database container may still be starting
            try:
                self.conn = psycopg.connect(url, autocommit=True, row_factory=dict_row)
                break
            except psycopg.OperationalError:
                if attempt == 14:
                    raise
                time.sleep(2)
        with self.conn.cursor() as cur:
            for statement in _SCHEMA.format(autoinc="BIGSERIAL PRIMARY KEY").split(";"):
                if statement.strip():
                    cur.execute(statement.replace(" REAL", " DOUBLE PRECISION"))

    def columns(self, table: str) -> set[str]:
        rows = self.query("SELECT column_name FROM information_schema.columns WHERE table_name=%s", [table])
        return {r["column_name"] for r in rows}

    def execute(self, sql: str, params: list) -> None:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def insert_returning_id(self, sql: str, params: list, id_column: str) -> int:
        with self.conn.cursor() as cur:
            cur.execute(f"{sql} RETURNING {id_column}", params)
            return cur.fetchone()[id_column]

    def query(self, sql: str, params: list) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def close(self) -> None:
        self.conn.close()


class Telemetry:
    def __init__(self, db_path: str | Path | None = None, content_max_chars: int = 4000, db_url: str | None = None):
        self.content_max_chars = content_max_chars
        self._lock = threading.Lock()
        db_url = db_url or os.environ.get("TELEMETRY_DB_URL")
        if db_url and db_url.startswith(("postgres://", "postgresql://")):
            self._db = _Postgres(db_url)
            self.backend = "postgresql"
        else:
            self._db = _SQLite(Path(db_path or "runtime/telemetry.db"))
            self.backend = "sqlite"
        self._migrate()
        self._subscribers: dict[str, list[asyncio.Queue]] = {}

    def _sql(self, sql: str) -> str:
        return sql.replace("?", self._db.placeholder) if self._db.placeholder != "?" else sql

    def _migrate(self) -> None:
        """Add columns introduced after a database was created (keeps old telemetry readable)."""
        for table, columns in (("runs", {"defence": "TEXT", "session_id": "TEXT", "turns": "INTEGER"}),
                               ("events", {"turn": "INTEGER"})):
            existing = self._db.columns(table)
            for name, kind in columns.items():
                if name not in existing:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}", [])

    def mark_interrupted(self) -> int:
        """Close runs a previous server left queued/running (it was stopped mid-run)."""
        stale = self._query("SELECT run_id FROM runs WHERE status IN ('queued', 'running')")
        for row in stale:
            self.update_run(row["run_id"], status="interrupted", ended_at=now_iso(),
                            error="the server stopped before this run finished")
        return len(stale)

    def sessions(self, limit: int = 50) -> list[dict]:
        """Chat conversations (manual prompts grouped by session), newest first."""
        rows = self._query("SELECT session_id, prompt, started_at, model, status FROM runs "
                           "WHERE label = 'manual' AND session_id IS NOT NULL ORDER BY started_at")
        by_session: dict[str, dict] = {}
        for r in rows:
            s = by_session.setdefault(r["session_id"], {"session_id": r["session_id"], "title": r["prompt"],
                                                         "started_at": r["started_at"], "turns": 0})
            s["turns"] += 1
            s["last_at"], s["model"] = r["started_at"], r["model"]
        return sorted(by_session.values(), key=lambda s: s["last_at"], reverse=True)[:limit]

    def clear(self) -> None:
        """Delete all telemetry (used by tests and by an explicit reset)."""
        with self._lock:
            self._db.execute("DELETE FROM events", [])
            self._db.execute("DELETE FROM runs", [])

    # ---- writing --------------------------------------------------------
    def log(self, event_type: str, **fields: Any) -> dict:
        row = {c: None for c in EVENT_COLUMNS if c != "event_id"}
        row["timestamp"] = now_iso()
        row["event_type"] = event_type
        for key, value in fields.items():
            if key in ("args", "details"):
                row[f"{key}_json"] = json.dumps(value, default=str) if value is not None else None
            elif key in row:
                row[key] = value
            else:
                raise KeyError(f"unknown telemetry field {key!r}")
        if isinstance(row["content"], str) and len(row["content"]) > self.content_max_chars:
            row["content"] = row["content"][: self.content_max_chars] + "...[truncated]"
        cols = list(row)
        with self._lock:
            row["event_id"] = self._db.insert_returning_id(
                self._sql(f"INSERT INTO events ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"),
                [row[c] for c in cols], "event_id",
            )
        self._publish(row.get("run_id"), {"kind": "event", "event": row})
        return row

    def start_run(self, **fields: Any) -> None:
        fields.setdefault("started_at", now_iso())
        fields.setdefault("status", "running")
        cols = [c for c in RUN_COLUMNS if c in fields]
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "run_id")
        with self._lock:
            self._db.execute(
                self._sql(f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                          f"ON CONFLICT (run_id) DO UPDATE SET {updates}"),
                [fields[c] for c in cols],
            )
        self._publish(fields["run_id"], {"kind": "run", "run": self.get_run(fields["run_id"])})

    def update_run(self, run_id: str, **fields: Any) -> None:
        for k in ("evaluation_json", "hooks_json"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], default=str)
        for k in ("task_success", "attack_success"):
            if isinstance(fields.get(k), bool):
                fields[k] = int(fields[k])
        cols = [c for c in fields if c in RUN_COLUMNS]
        with self._lock:
            self._db.execute(
                self._sql(f"UPDATE runs SET {', '.join(f'{c}=?' for c in cols)} WHERE run_id=?"),
                [fields[c] for c in cols] + [run_id],
            )
        run = self.get_run(run_id)
        self._publish(run_id, {"kind": "run", "run": run})
        if run and run.get("status") != "running":
            self._publish(run_id, {"kind": "done", "run": run})

    # ---- reading --------------------------------------------------------
    def _query(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return self._db.query(self._sql(sql), list(params))

    def get_run(self, run_id: str) -> dict | None:
        rows = self._query("SELECT * FROM runs WHERE run_id=?", [run_id])
        return rows[0] if rows else None

    def list_runs(self, limit: int = 200, batch_id: str | None = None) -> list[dict]:
        if batch_id:
            return self._query("SELECT * FROM runs WHERE batch_id=? ORDER BY started_at DESC LIMIT ?", [batch_id, limit])
        return self._query("SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", [limit])

    def events(self, run_id: str | None = None, since_id: int = 0) -> list[dict]:
        if run_id:
            return self._query("SELECT * FROM events WHERE run_id=? AND event_id>? ORDER BY event_id", [run_id, since_id])
        return self._query("SELECT * FROM events WHERE event_id>? ORDER BY event_id", [since_id])

    def summary(self, batch_id: str | None = None) -> dict:
        """SRS Ch. 13 metrics per (model, defence configuration, label, category).

        attack_success_rate  attack runs whose attacker goal was reached / attack runs
        task_success_rate    runs whose task checks all passed / runs with task checks
        unsafe_action_rate   runs with an executed out-of-scope call or a reached attacker goal / runs
        tool_misuse_rate     tool calls outside the task's authorised scope / tool calls
        recovery_rate        out-of-scope calls that were contained (not executed) / out-of-scope calls
        false_blocking_rate  (benign runs) actions blocked or held for review / actions
        avg_run_ms, avg_hook_ms   wall time per run, time spent inside the security layer per run
        Detector precision/recall/F1 and CPU/GPU overhead are computed by the ML pipeline.
        """
        where, params = ("WHERE batch_id=? AND status NOT IN ('queued','running')", [batch_id]) if batch_id \
            else ("WHERE status NOT IN ('queued','running')", [])
        runs = self._query(f"SELECT * FROM runs {where}", params)
        if not runs:
            return {"groups": []}
        ids = [r["run_id"] for r in runs]
        marks = ",".join("?" * len(ids))
        actions = {r["run_id"]: r for r in self._query(
            f"""SELECT run_id,
                       SUM(CASE WHEN event_type IN ('input_received','tool_call','tool_result','memory_write','memory_read','output_generated') THEN 1 ELSE 0 END) AS actions,
                       SUM(CASE WHEN final_decision IN ('BLOCK','REVIEW') THEN 1 ELSE 0 END) AS blocked
                FROM events WHERE run_id IN ({marks}) GROUP BY run_id""", ids)}
        hook_ms: dict[str, float] = {}
        for e in self._query(f"SELECT run_id, details_json FROM events WHERE run_id IN ({marks}) AND details_json LIKE ?",
                             ids + ["%hook_ms%"]):
            hook_ms[e["run_id"]] = hook_ms.get(e["run_id"], 0.0) + float(json.loads(e["details_json"]).get("hook", {}).get("hook_ms", 0))

        def rate(num, den):
            return None if not den else round(num / den, 4)

        groups: dict[tuple, list[dict]] = {}
        for r in runs:
            groups.setdefault((r["model"], r["defence"] or "baseline", r["label"], r["category"]), []).append(r)
        out = []
        for (model, defence, label, category), rs in sorted(groups.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
            ev = [json.loads(r["evaluation_json"] or "{}") for r in rs]
            task_runs = [r for r in rs if r["task_success"] is not None]
            attack_runs = [r for r in rs if r["attack_success"] is not None]
            scoped = [e for e in ev if "tool_calls_outside_scope" in e]
            outside = sum(e["tool_calls_outside_scope"] for e in scoped)
            executed_outside = sum(e["executed_outside_scope"] for e in scoped)
            unsafe = sum(1 for r, e in zip(rs, ev) if r["attack_success"] or e.get("executed_outside_scope"))
            durations = [(datetime.fromisoformat(r["ended_at"]) - datetime.fromisoformat(r["started_at"])).total_seconds() * 1000
                         for r in rs if r["ended_at"] and r["started_at"]]
            out.append({
                "model": model, "defence": defence, "label": label, "category": category, "runs": len(rs),
                "task_success_rate": rate(sum(r["task_success"] for r in task_runs), len(task_runs)),
                "attack_success_rate": rate(sum(r["attack_success"] for r in attack_runs), len(attack_runs)),
                "unsafe_action_rate": rate(unsafe, len(rs)),
                "tool_misuse_rate": rate(outside, sum(e["tool_calls"] for e in scoped)),
                "recovery_rate": rate(outside - executed_outside, outside),
                "false_blocking_rate": rate(sum((actions.get(r["run_id"]) or {}).get("blocked") or 0 for r in rs),
                                            sum((actions.get(r["run_id"]) or {}).get("actions") or 0 for r in rs))
                if label == "benign" else None,
                "avg_run_ms": round(sum(durations) / len(durations), 1) if durations else None,
                "avg_hook_ms": round(sum(hook_ms.get(r["run_id"], 0.0) for r in rs) / len(rs), 3),
            })
        return {"groups": out}

    # ---- export (FR-23) ---------------------------------------------------
    def export(self, fmt: str = "csv", table: str = "events", run_id: str | None = None) -> str:
        if table == "runs":
            rows = self._query("SELECT * FROM runs WHERE run_id=?", [run_id]) if run_id else self._query("SELECT * FROM runs ORDER BY started_at")
            columns = RUN_COLUMNS
        else:
            rows = self.events(run_id)
            columns = EVENT_COLUMNS
        if fmt == "json":
            return json.dumps(rows, indent=2, default=str)
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return buf.getvalue()

    # ---- live streaming (FR-29) --------------------------------------------
    def subscribe(self, run_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(run_id, []).append(q)
        return q

    def unsubscribe(self, run_id: str, q: asyncio.Queue) -> None:
        subs = self._subscribers.get(run_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subscribers.pop(run_id, None)

    def _publish(self, run_id: str | None, message: dict) -> None:
        for key in (run_id, "*"):
            for q in self._subscribers.get(key, []) if key else []:
                q.put_nowait(message)

    def close(self) -> None:
        with self._lock:
            self._db.close()
