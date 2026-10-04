"""Tool gateway (PRD FR-9): EVERY tool call goes through `ToolGateway.execute_tool(name, args, context)`.

Order of operations for one call:
    1. look up tool metadata (FR-10) and name the concrete resource
    2. before_tool extension point          (future Tool Guard / policy engine)
    3. execute in the sandbox (or agent-side memory tools)
    4. before_input extension point on the output, tagged with its provenance source
       (the output is content the agent is about to read - future Input Guard)
    5. telemetry: tool_call + tool_result events
"""
from __future__ import annotations

import time

from .context import RunContext
from .hooks import before_input, before_tool
from .memory import LongTermMemory
from .sandbox.tools import ToolSpec, _obj

MEMORY_TOOLS = [
    ToolSpec("remember", "SAVE a fact, note or preference to long-term memory so it is available in future "
             "conversations. Use this when the user asks you to remember something.",
             _obj({"content": {"type": "string"}}, ["content"]),
             "medium", ["memory"], "long_term_memory", "tool_output", "long_term_memory", location="agent"),
    ToolSpec("recall", "LOOK UP notes previously saved in long-term memory (read-only; does not save anything).",
             _obj({"query": {"type": "string"}}, ["query"]),
             "low", ["memory"], "long_term_memory", "memory", "long_term_memory", location="agent"),
]


class ToolGateway:
    def __init__(self, sandbox, memory: LongTermMemory):
        self.sandbox = sandbox
        self.memory = memory
        self.specs: dict[str, ToolSpec] = {}

    async def refresh(self) -> None:
        self.specs = {}
        for meta in await self.sandbox.list_tools():
            self.specs[meta["name"]] = ToolSpec(**meta)
        for spec in MEMORY_TOOLS:
            self.specs[spec.name] = spec

    def schemas(self, allowed: list[str] | None = None) -> list[dict]:
        """OpenAI-style tool schemas offered to the model (all tools unless the task narrows them)."""
        return [s.openai_schema() for n, s in self.specs.items() if not allowed or n in allowed]

    def metadata(self) -> list[dict]:
        return [s.metadata() for s in self.specs.values()]

    async def execute_tool(self, name: str, args: dict, context: RunContext) -> str:
        """Run one tool call and return the text handed back to the model."""
        spec = self.specs.get(name)
        meta = spec.metadata() if spec else {"name": name, "unknown": True}
        resource = spec.resource_for(args or {}) if spec else None

        # 2. before_tool extension point
        request = {"name": name, "args": dict(args or {}), "resource": resource}
        decision = await before_tool(request, context.hook_ctx("agent", tool=meta), context.hooks)
        if isinstance(decision.payload, dict):
            args = decision.payload.get("args", args)
        context.log("tool_call", hook=decision, tool_called=name, resource_accessed=resource,
                    provenance_source="agent", trust_level=context.trust("agent"), args=args,
                    outcome="dispatched" if decision.allowed else decision.decision.value.lower(),
                    details={"risk_level": meta.get("risk_level"), "location": meta.get("location")})
        if not decision.allowed:
            reason = decision.reason or "denied by policy"
            return f"[tool call {decision.decision.value.lower()}ed by the security layer: {reason}]"

        if spec is None:
            text, ok, duration = f"ERROR: unknown tool {name!r}", False, 0.0
        elif spec.location == "agent":
            start = time.perf_counter()
            text, ok = await self._memory_tool(name, args or {}, context)
            duration = round((time.perf_counter() - start) * 1000, 2)
        else:
            result = await self.sandbox.execute(name, args or {})
            text, ok, duration = result["output"], result["ok"], result["duration_ms"]

        # 4. tool output is content the agent is about to read -> before_input
        source = spec.output_source if spec else "tool_output"
        screened = await before_input(text, context.hook_ctx(source, tool=meta, extra={"tool_output": True}), context.hooks)
        if screened.allowed:
            text = screened.payload if isinstance(screened.payload, str) else text
        else:
            text = f"[tool output withheld by the security layer: {screened.reason or 'flagged'}]"
        context.log("tool_result", hook=screened, guard_column="input_guard_result", tool_called=name,
                    resource_accessed=resource, provenance_source=source, trust_level=context.trust(source),
                    outcome="success" if ok else "error", latency_ms=duration, content=text)
        return text

    async def _memory_tool(self, name: str, args: dict, context: RunContext) -> tuple[str, bool]:
        if name == "remember":
            content = str(args.get("content", "")).strip()
            if not content:
                return "ERROR: nothing to remember", False
            # The text was produced by the model. Record which kinds of content it had read before
            # writing (raw provenance data for the security layer; nothing is decided here).
            seen = sorted({t["provenance_source"] for t in context.trace
                           if t.get("event_type") in ("tool_result", "memory_read") and t.get("provenance_source")})
            result = await (context.memory or self.memory).write_memory(content, source="tool_output", ctx=context,
                                                    metadata={"context_sources": seen})
            return ("Saved to long-term memory." if result["stored"] else f"Memory write not stored: {result.get('reason')}"), result["stored"]
        if name == "recall":
            hits = (context.memory or self.memory).search(str(args.get("query", "")), k=5)
            context.log("memory_read", resource_accessed="long_term_memory", provenance_source="memory",
                        trust_level=context.trust("memory"), outcome="success", content=f"{len(hits)} memories")
            return ("\n".join(f"- {h['content']} (source: {h['source']})" for h in hits) or "No memories."), True
        return f"ERROR: unknown memory tool {name}", False
