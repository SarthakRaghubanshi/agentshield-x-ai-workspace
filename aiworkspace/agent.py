"""The agent loop (PRD FR-6): task -> plan -> tool call -> observe -> repeat -> final answer.

This file contains no security logic. Inputs, tool calls, memory writes and outputs flow through
the extension points (`aiworkspace.hooks`) and the tool gateway (`execute_tool`).
"""
from __future__ import annotations

from .context import RunContext
from .gateway import ToolGateway
from .hooks import before_input, before_output
from .memory import LongTermMemory
from .models import ModelClient


class Agent:
    def __init__(self, model_client: ModelClient, gateway: ToolGateway, memory: LongTermMemory, cfg: dict):
        self.models = model_client
        self.gateway = gateway
        self.memory = memory
        acfg = cfg.get("agent", {})
        self.max_steps = int(acfg.get("max_steps", 8))
        self.system_template = acfg.get("system_prompt", "You are a helpful assistant.")
        self.canary = acfg.get("canary", "")
        self.recall_top_k = int((cfg.get("memory") or {}).get("recall_top_k", 3))

    def system_prompt(self, memories: list[dict]) -> str:
        prompt = self.system_template.replace("{canary}", self.canary).strip()
        if memories:
            notes = "\n".join(f"- {m['content']}" for m in memories)
            prompt += f"\n\nNotes from long-term memory:\n{notes}"
        return prompt

    async def run(self, prompt: str, ctx: RunContext, allowed_tools: list[str] | None = None) -> dict:
        """Run one task to completion. Returns {"output", "status", "steps"}."""
        ctx.log("task_start", provenance_source="user", trust_level=ctx.trust("user"), content=prompt,
                details={"max_steps": self.max_steps, "allowed_tools": allowed_tools})

        # User prompt -> before_input
        screened = await before_input(prompt, ctx.hook_ctx("user"), ctx.hooks)
        ctx.log("input_received", hook=screened, guard_column="input_guard_result", provenance_source="user",
                trust_level=ctx.trust("user"), content=prompt,
                outcome="accepted" if screened.allowed else screened.decision.value.lower())
        if not screened.allowed:
            return await self._finish(ctx, f"[request blocked by the security layer: {screened.reason or 'flagged'}]", "blocked")
        prompt = screened.payload if isinstance(screened.payload, str) else prompt

        # Long-term memory recalled into the context -> before_input (source=memory)
        memories = []
        for mem in self.memory.search(prompt, k=self.recall_top_k) if self.recall_top_k else []:
            mem_screen = await before_input(mem["content"], ctx.hook_ctx("memory", extra={"memory_id": mem["id"]}), ctx.hooks)
            ctx.log("memory_read", hook=mem_screen, guard_column="input_guard_result", resource_accessed="long_term_memory",
                    provenance_source=mem["source"], trust_level=mem["trust_level"], content=mem["content"],
                    outcome="loaded" if mem_screen.allowed else "withheld", details={"memory_id": mem["id"]})
            if mem_screen.allowed:
                memories.append(mem | {"content": mem_screen.payload if isinstance(mem_screen.payload, str) else mem["content"]})

        messages: list[dict] = [
            {"role": "system", "content": self.system_prompt(memories)},
            {"role": "user", "content": prompt},
        ]
        tools = self.gateway.schemas(allowed_tools)

        for step in range(1, self.max_steps + 1):
            ctx.step = step
            try:
                reply = await self.models.complete(ctx.model, messages, tools)
            except Exception as exc:
                ctx.log("error", outcome="model_error", content=f"{type(exc).__name__}: {exc}")
                return await self._finish(ctx, f"[model error: {type(exc).__name__}: {exc}]", "error", skip_hook=True)
            ctx.log("model_call", outcome="tool_calls" if reply.tool_calls else "final",
                    latency_ms=reply.latency_ms, prompt_tokens=reply.prompt_tokens,
                    completion_tokens=reply.completion_tokens, total_tokens=reply.total_tokens, cost_usd=reply.cost_usd,
                    content=reply.content or "", details={"tool_calls": [{"name": c["name"], "args": c["arguments"]} for c in reply.tool_calls],
                                                          "provider_model": reply.model})
            messages.append(reply.raw_message)

            if not reply.tool_calls:
                return await self._finish(ctx, reply.content or "", "completed", steps=step)

            for call in reply.tool_calls:
                result = await self.gateway.execute_tool(call["name"], call["arguments"], ctx)
                messages.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": result})

        return await self._finish(ctx, "[stopped: maximum number of steps reached without a final answer]",
                                  "max_steps", steps=self.max_steps)

    async def _finish(self, ctx: RunContext, output: str, status: str, steps: int | None = None, skip_hook: bool = False) -> dict:
        released = output
        outcome = None
        if not skip_hook:
            outcome = await before_output(output, ctx.hook_ctx("agent"), ctx.hooks)
            if outcome.allowed:
                released = outcome.payload if isinstance(outcome.payload, str) else output
            else:
                released = f"[response {outcome.decision.value.lower()}ed by the security layer: {outcome.reason or 'flagged'}]"
                status = "blocked" if status == "completed" else status
        ctx.log("output_generated", hook=outcome, guard_column="output_guard_result", provenance_source="agent",
                trust_level=ctx.trust("agent"), content=released,
                outcome=status, details={"original_output": output} if released != output else None)
        return {"output": released, "status": status, "steps": steps or ctx.step}
