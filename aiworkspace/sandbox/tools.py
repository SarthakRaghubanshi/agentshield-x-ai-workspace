"""Native sandbox tools (PRD FR-7, FR-10).

Every tool declares metadata: name, risk level, resources touched, allowed scope and the
provenance tag of its output. Tools only touch the sandbox directory - nothing reaches a real
system: email/messages go to an outbox file, web search and fetch use local fixtures, the
database is a read-only synthetic SQLite file.

These are plain functions; they are executed only via `SandboxRuntime.execute()`, which is
reached only via the `execute_tool()` gateway in the agent process.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict
    risk_level: str                     # low | medium | high
    resources: list[str]                # resource classes touched, e.g. ["filesystem"]
    scope: str                          # allowed scope, e.g. "files/**"
    output_source: str = "tool_output"  # provenance tag of what the tool returns (FR-20)
    resource_template: str = ""         # how to name the concrete resource, e.g. "file:{path}"
    location: str = "sandbox"           # sandbox | agent | mcp
    extra: dict = field(default_factory=dict)

    def metadata(self) -> dict:
        return asdict(self)

    def openai_schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def resource_for(self, args: dict) -> str | None:
        if not self.resource_template:
            return self.resources[0] if self.resources else None
        try:
            return self.resource_template.format_map(_Default(args))
        except Exception:
            return self.resource_template


class _Default(dict):
    def __missing__(self, key):
        return "?"


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": props, "required": required or []}


class ToolError(Exception):
    """Raised for a tool failure that should be reported back to the agent as text."""


class NativeTools:
    """Implementation of the built-in tools against one sandbox root directory."""

    def __init__(self, root: Path, rag=None):
        self.root = Path(root)
        self.rag = rag
        self.specs: dict[str, ToolSpec] = {}
        self.impls: dict[str, Callable[..., str]] = {}
        self._register_all()

    # ---- helpers ----------------------------------------------------------
    @property
    def files_dir(self) -> Path:
        return self.root / "files"

    def _resolve(self, path: str) -> Path:
        """Resolve a user path inside files/ (sandbox confinement, not a security policy)."""
        p = (path or "").strip().replace("\\", "/")
        p = re.sub(r"^(\./|/)?(sandbox/)?(files/)?", "", p)
        target = (self.files_dir / p).resolve()
        if not str(target).startswith(str(self.files_dir.resolve())):
            raise ToolError(f"path {path!r} is outside the sandbox workspace")
        return target

    def _add(self, spec: ToolSpec, impl: Callable[..., str]) -> None:
        self.specs[spec.name] = spec
        self.impls[spec.name] = impl

    # ---- registration ---------------------------------------------------------
    def _register_all(self) -> None:
        self._add(ToolSpec(
            "list_files", "List files in the workspace (optionally under a sub-directory).",
            _obj({"directory": {"type": "string", "description": "sub-directory, default is the workspace root"}}),
            "low", ["filesystem"], "files/**", "tool_output", "dir:{directory}"), self.list_files)
        self._add(ToolSpec(
            "read_file", "Read a text, CSV, Markdown or PDF file from the workspace and return its text.",
            _obj({"path": {"type": "string", "description": "file path relative to the workspace, e.g. report.pdf"}}, ["path"]),
            "medium", ["filesystem"], "files/**", "document", "file:{path}"), self.read_file)
        self._add(ToolSpec(
            "write_file", "Create or overwrite a text file in the workspace.",
            _obj({"path": {"type": "string"}, "content": {"type": "string"}}, ["path", "content"]),
            "high", ["filesystem"], "files/**", "tool_output", "file:{path}"), self.write_file)
        self._add(ToolSpec(
            "calculator", "Evaluate an arithmetic expression, e.g. '0.15 * 2340' or 'sqrt(2) ** 3'.",
            _obj({"expression": {"type": "string"}}, ["expression"]),
            "low", ["compute"], "*", "tool_output", "compute"), self.calculator)
        self._add(ToolSpec(
            "web_search", "Search the web (controlled mock: results come from a fixed sandbox index).",
            _obj({"query": {"type": "string"}}, ["query"]),
            "medium", ["network:mock"], "web fixtures", "retrieved", "web_search:{query}"), self.web_search)
        self._add(ToolSpec(
            "fetch_url", "Fetch the text of a web page by URL (controlled mock; sandbox has no internet).",
            _obj({"url": {"type": "string"}}, ["url"]),
            "medium", ["network:mock"], "web fixtures", "retrieved", "url:{url}"), self.fetch_url)
        self._add(ToolSpec(
            "query_database",
            "Run a read-only SQLite query on the company database. Schema: "
            "customers(id, name, email, region, plan, card_number, account_pin); "
            "orders(id, customer_id, region, amount_inr, order_date). orders.customer_id = customers.id.",
            _obj({"sql": {"type": "string", "description": "a SELECT statement"}}, ["sql"]),
            "medium", ["database"], "db: customers, orders (read-only)", "tool_output", "database"), self.query_database)
        self._add(ToolSpec(
            "list_inbox", "List emails in the user's inbox (id, sender, subject, date), newest first.",
            _obj({"limit": {"type": "integer", "description": "maximum number of emails, default 20"}}), "low", ["email:inbox"], "inbox", "tool_output", "inbox"), self.list_inbox)
        self._add(ToolSpec(
            "read_email", "Read one email from the inbox by id.",
            _obj({"email_id": {"type": "string"}}, ["email_id"]),
            "low", ["email:inbox"], "inbox", "document", "email:{email_id}"), self.read_email)
        self._add(ToolSpec(
            "send_email", "Send an email (mock: delivered to the sandbox outbox only).",
            _obj({"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}}, ["to", "subject", "body"]),
            "high", ["email:outbound"], "outbox (mock)", "tool_output", "email_to:{to}"), self.send_email)
        self._add(ToolSpec(
            "send_message", "Post a chat message to a channel or person (mock: sandbox outbox only).",
            _obj({"channel": {"type": "string"}, "text": {"type": "string"}}, ["channel", "text"]),
            "high", ["messaging:outbound"], "outbox (mock)", "tool_output", "message_to:{channel}"), self.send_message)
        self._add(ToolSpec(
            "search_knowledge_base", "Search the internal knowledge base (policies, FAQs) and return the most relevant passages.",
            _obj({"query": {"type": "string"}, "k": {"type": "integer", "description": "number of passages, default 3"}}, ["query"]),
            "low", ["rag"], "kb/**", "retrieved", "kb_search:{query}"), self.search_knowledge_base)

    # ---- implementations --------------------------------------------------
    def list_files(self, directory: str = "") -> str:
        base = self._resolve(directory or "")
        if not base.exists():
            top = sorted(p.relative_to(self.files_dir).as_posix() for p in self.files_dir.rglob("*") if p.is_file())
            raise ToolError(f"directory not found: {directory}. Files in the workspace: {', '.join(top)}")
        items = sorted(p.relative_to(self.files_dir).as_posix() for p in base.rglob("*") if p.is_file())
        return "\n".join(items) or "(empty)"

    def read_file(self, path: str) -> str:
        target = self._resolve(path)
        if not target.is_file():
            raise ToolError(f"file not found: {path}")
        if target.suffix.lower() == ".pdf":
            text = "\n".join((page.extract_text() or "") for page in PdfReader(str(target)).pages)
        else:
            text = target.read_text(encoding="utf-8", errors="replace")
        return text[:20000]

    def write_file(self, path: str, content: str) -> str:
        target = self._resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return f"wrote {len(content)} characters to {target.relative_to(self.files_dir).as_posix()}"

    _OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
            ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
            ast.USub: operator.neg, ast.UAdd: operator.pos}
    _FUNCS = {n: getattr(math, n) for n in ("sqrt", "log", "log10", "exp", "sin", "cos", "tan", "floor", "ceil")} | {
        "abs": abs, "round": round, "min": min, "max": max, "pi": math.pi, "e": math.e}

    def calculator(self, expression: str) -> str:
        expr = expression.replace(",", "").replace("^", "**")

        def ev(node):
            if isinstance(node, ast.Expression):
                return ev(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in self._OPS:
                left, right = ev(node.left), ev(node.right)
                if isinstance(node.op, ast.Pow) and abs(right) > 100:
                    raise ToolError("exponent too large")
                return self._OPS[type(node.op)](left, right)
            if isinstance(node, ast.UnaryOp) and type(node.op) in self._OPS:
                return self._OPS[type(node.op)](ev(node.operand))
            if isinstance(node, ast.Name) and node.id in self._FUNCS:
                return self._FUNCS[node.id]
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in self._FUNCS:
                return self._FUNCS[node.func.id](*[ev(a) for a in node.args])
            raise ToolError(f"unsupported expression element: {ast.dump(node)[:60]}")

        try:
            result = ev(ast.parse(expr, mode="eval"))
        except ToolError:
            raise
        except Exception as exc:
            raise ToolError(f"could not evaluate {expression!r}: {exc}") from exc
        if isinstance(result, float):
            result = round(result, 6)
        return f"{expression} = {result}"

    def _pages(self) -> list[dict]:
        """One JSON file per web page under web/ (attack tasks add pages via setup.files)."""
        return [json.loads(f.read_text(encoding="utf-8")) for f in sorted((self.root / "web").glob("*.json"))]

    def web_search(self, query: str) -> str:
        terms = set(re.findall(r"\w+", query.lower()))
        scored = []
        for page in self._pages():
            words = re.findall(r"\w+", f"{page['title']} {page['snippet']} {page['content']}".lower())
            score = sum(1 for w in words if w in terms)
            if score:
                scored.append((score, page))
        scored.sort(key=lambda x: -x[0])
        if not scored:
            return "No results."
        return "\n\n".join(
            f"[{i + 1}] {p['title']}\nURL: {p['url']}\n{p['snippet']}\n{p['content']}" for i, (_, p) in enumerate(scored[:3])
        )

    def fetch_url(self, url: str) -> str:
        for page in self._pages():
            if page["url"].rstrip("/") == url.strip().rstrip("/"):
                return f"{page['title']}\n\n{page['content']}"
        raise ToolError(f"404: {url} is not available (the sandbox has no internet access)")

    def query_database(self, sql: str) -> str:
        db = self.root / "db" / "customers.db"
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            cur = conn.execute(sql)
            cols = [d[0] for d in cur.description or []]
            rows = cur.fetchmany(50)
        except sqlite3.Error as exc:
            raise ToolError(f"SQL error: {exc}") from exc
        finally:
            conn.close()
        if not cols:
            return "(no result set)"
        lines = [" | ".join(cols)] + [" | ".join(str(v) for v in r) for r in rows]
        return "\n".join(lines)

    def _inbox(self) -> list[dict]:
        """One JSON file per email under inbox/, newest first."""
        emails = [json.loads(f.read_text(encoding="utf-8")) for f in (self.root / "inbox").glob("*.json")]
        return sorted(emails, key=lambda m: (m["date"], m["id"]), reverse=True)

    def list_inbox(self, limit: int = 20) -> str:
        emails = self._inbox()[: int(limit or 20)]
        return "\n".join(f"{m['id']} | {m['date']} | from {m['from']} | {m['subject']}" for m in emails)

    def read_email(self, email_id: str) -> str:
        for m in self._inbox():
            if m["id"] == email_id.strip():
                return f"From: {m['from']}\nDate: {m['date']}\nSubject: {m['subject']}\n\n{m['body']}"
        raise ToolError(f"no email with id {email_id}")

    def _append_outbox(self, name: str, record: dict) -> None:
        out = self.root / "outbox"
        out.mkdir(exist_ok=True)
        with open(out / name, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def send_email(self, to: str, subject: str, body: str) -> str:
        self._append_outbox("emails.jsonl", {"to": to, "subject": subject, "body": body})
        return f"Email to {to} queued in the sandbox outbox (mock - nothing leaves the sandbox)."

    def send_message(self, channel: str, text: str) -> str:
        self._append_outbox("messages.jsonl", {"channel": channel, "text": text})
        return f"Message posted to {channel} (mock outbox)."

    def search_knowledge_base(self, query: str, k: int = 3) -> str:
        if self.rag is None:
            raise ToolError("knowledge base is not available")
        hits = self.rag.search(query, k=int(k or 3))
        if not hits:
            return "No relevant passages."
        return "\n\n".join(f"[{h['doc']}] (score {h['score']:.2f})\n{h['text']}" for h in hits)
