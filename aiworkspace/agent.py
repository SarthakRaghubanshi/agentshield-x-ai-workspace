"""The agent loop (PRD FR-6): task -> plan -> tool call -> observe -> repeat -> final answer.

This file contains no security logic. Inputs, tool calls, memory writes and outputs flow through
the extension points (`aiworkspace.hooks`) and the tool gateway (`execute_tool`).

One call to `Agent.run()` handles one user turn. Short-term memory (FR-14) is the conversation:
pass the `messages` returned by one turn as `history` to the next.
"""
from __future__ import annotations

import json
import re
import uuid

from .context import RunContext
from .gateway import ToolGateway
from .hooks import before_input, before_output
from .memory import LongTermMemory
from .models import ModelClient, ModelReply

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_TOOL_CALL_TAG = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S | re.I)
_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.S)


def clean_answer(text: str | None) -> str:
    """Remove reasoning blocks some local models (e.g. qwen3) leave in their answer."""
    return _THINK.sub("", text or "").strip()


def text_tool_calls(content: str | None, tool_names: set[str]) -> list[dict]:
    """Recover tool calls that a small model wrote as text instead of a structured tool call.

    Accepts a bare JSON object/list, a ```json fence, or <tool_call>...</tool_call> tags, each
    shaped {"name": ..., "arguments"|"parameters": {...}}. Only known tool names are accepted.
    """
    text = clean_answer(content)
    if not text:
        return []
    blobs = _TOOL_CALL_TAG.findall(text) or [_FENCE.sub(r"\1", text)]
    calls = []
    for blob in blobs:
        try:
            data = json.loads(blob)
        except (json.JSONDecodeError, TypeError):
            return []
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or item.get("name") not in tool_names:
                return []
            args = item.get("arguments", item.get("parameters", {}))
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            calls.append({"id": f"call_{uuid.uuid4().hex[:8]}", "name": item["name"], "arguments": args or {},
                          "raw_arguments": json.dumps(args or {})})
    return calls


class Agent:
    def __init__(self, model_client: ModelClient, gateway: ToolGateway, memory: LongTermMemory, cfg: dict):
        self.models = model_client
        self.gateway = gateway
        self.memory = memory
        acfg = cfg.get("agent", {})
        self.max_steps = int(acfg.get("max_steps", 10))
        self.system_template = acfg.get("system_prompt", "You are a helpful assistant.")
        self.canary = acfg.get("canary", "")
        self.parse_text_tool_calls = bool(acfg.get("parse_text_tool_calls", True))
        self.recall_top_k = int((cfg.get("memory") or {}).get("recall_top_k", 3))

    def system_prompt(self, memories: list[dict]) -> str:
        prompt = self.system_template.replace("{canary}", self.canary).strip()
        if memories:
            notes = "\n".join(f"- {m['content']}" for m in memories)
            prompt += f"\n\nNotes from long-term memory:\n{notes}"
        return prompt

    async def run(self, prompt: str, ctx: RunContext, allowed_tools: list[str] | None = None,
                  history: list[dict] | None = None) -> dict:
        """Run one user turn to completion.

        Returns {"output", "status", "steps", "messages"}; `messages` is the conversation after
        this turn (without the system prompt), to be passed as `history` to the next turn.
        """
        conversation = list(history or [])
        start_step = ctx.step

        # User prompt -> before_input
        screened = await before_input(prompt, ctx.hook_ctx("user"), ctx.hooks)
        ctx.log("input_received", hook=screened, guard_column="input_guard_result", provenance_source="user",
                trust_level=ctx.trust("user"), content=prompt,
                outcome="accepted" if screened.allowed else screened.decision.value.lower(),
                details={"history_messages": len(conversation)})
        if not screened.allowed:
            notice = f"[request blocked by the security layer: {screened.reason or 'flagged'}]"
            result = await self._finish(ctx, notice, "blocked", start_step)
            result["messages"] = conversation + [{"role": "user", "content": prompt},
                                                 {"role": "assistant", "content": result["output"]}]
            return result
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

        conversation.append({"role": "user", "content": prompt})
        tools = self.gateway.schemas(allowed_tools)
        tool_names = {t["function"]["name"] for t in tools}

        for step in range(start_step + 1, start_step + self.max_steps + 1):
            ctx.step = step
            messages = [{"role": "system", "content": self.system_prompt(memories)}] + conversation
            try:
                reply = await self.models.complete(ctx.model, messages, tools)
            except Exception as exc:
                ctx.log("error", outcome="model_error", content=f"{type(exc).__name__}: {exc}")
                result = await self._finish(ctx, f"[model error: {type(exc).__name__}: {exc}]", "error", start_step, skip_hook=True)
                result["messages"] = conversation
                return result
            parsed_from_text = False
            if not reply.tool_calls and self.parse_text_tool_calls:
                recovered = text_tool_calls(reply.content, tool_names)
                if recovered:
                    reply = self._with_tool_calls(reply, recovered)
                    parsed_from_text = True
            ctx.log("model_call", outcome="tool_calls" if reply.tool_calls else "final",
                    latency_ms=reply.latency_ms, prompt_tokens=reply.prompt_tokens,
                    completion_tokens=reply.completion_tokens, total_tokens=reply.total_tokens, cost_usd=reply.cost_usd,
                    content=reply.content or "",
                    details={"tool_calls": [{"name": c["name"], "args": c["arguments"]} for c in reply.tool_calls],
                             "provider_model": reply.model, "tool_calls_parsed_from_text": parsed_from_text})

            if not reply.tool_calls:
                result = await self._finish(ctx, clean_answer(reply.content), "completed", start_step)
                # The conversation keeps what was actually released (after any output redaction).
                conversation.append({"role": "assistant", "content": result["output"]})
                result["messages"] = conversation
                return result

            conversation.append(reply.raw_message)
            for call in reply.tool_calls:
                output = await self.gateway.execute_tool(call["name"], call["arguments"], ctx)
                conversation.append({"role": "tool", "tool_call_id": call["id"], "name": call["name"], "content": output})

        result = await self._finish(ctx, "[stopped: maximum number of steps reached without a final answer]",
                                    "max_steps", start_step)
        conversation.append({"role": "assistant", "content": result["output"]})
        result["messages"] = conversation
        return result

    @staticmethod
    def _with_tool_calls(reply: ModelReply, calls: list[dict]) -> ModelReply:
        reply.tool_calls = calls
        reply.raw_message = {"role": "assistant", "content": None, "tool_calls": [
            {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["raw_arguments"]}}
            for c in calls]}
        return reply

    async def _finish(self, ctx: RunContext, output: str, status: str, start_step: int, skip_hook: bool = False) -> dict:
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
        return {"output": released, "status": status, "steps": ctx.step - start_step}
