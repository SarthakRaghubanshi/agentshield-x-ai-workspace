"""Extension points for the AgentShield-X security layer (PRD FR-17..FR-20, NFR-1, NFR-5).

The agent routes every input, tool call, memory write and final output through exactly four
pass-through functions:

    before_input(payload, ctx)         user prompt, and any content the agent is about to read
    before_tool(payload, ctx)          {"name": ..., "args": {...}} before a tool executes
    before_memory_write(payload, ctx)  {"content": ..., "source": ..., "trust_level": ...}
    before_output(payload, ctx)        the final answer before it is released

By default they do nothing (and are switched OFF in config/workspace.yaml). This module contains
NO guard, policy, scoring or detection logic. A security layer attaches by registering handlers:

    # my_guards.py
    from aiworkspace.hooks import HookResult, Decision
    def tool_guard(payload, ctx):
        if payload["name"] == "read_file" and "protected" in payload["args"].get("path", ""):
            return HookResult(decision=Decision.BLOCK, reason="not authorised", authorisation_result="DENY")
    def register(manager):
        manager.register("before_tool", tool_guard)

and listing "my_guards:register" under hooks.plugins in config/workspace.yaml (or the
AGENTSHIELD_PLUGINS env var). No agent code changes are needed.

Handler contract (FROZEN - see docs/INTEGRATION.md):
    handler(payload, ctx: HookContext) -> HookResult | None      (sync or async)
    Returning None means "allow, unchanged". Returning HookResult(payload=...) replaces the payload
    (e.g. redaction). BLOCK stops the chain; REVIEW is recorded and the chain continues.
"""
from __future__ import annotations

import importlib
import inspect
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Union

log = logging.getLogger("aiworkspace.hooks")

HOOK_POINTS = ("before_input", "before_tool", "before_memory_write", "before_output")


class Decision(str, Enum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    REVIEW = "REVIEW"


@dataclass
class HookContext:
    """Everything a security component may need to judge one action. Read-only for handlers."""

    point: str
    run_id: str
    task_id: str
    agent_id: str
    step: int
    source: str                       # provenance tag: user | document | retrieved | tool_output | memory
    trust_level: str
    model: str = ""
    task: dict = field(default_factory=dict)       # label, category, prompt, allowed_tools, allowed_resources
    tool: dict | None = None                       # tool metadata (FR-10) for before_tool / tool outputs
    trace: list = field(default_factory=list)      # prior actions in this run: [{"event_type", "tool", "source", ...}]
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class HookResult:
    """What a handler returns. Every field is optional; telemetry columns are filled from it."""

    decision: Decision = Decision.ALLOW
    payload: Any = None                      # replacement payload; None = unchanged
    reason: str = ""
    guard_result: str | None = None          # -> input_guard_result / output_guard_result columns
    authorisation_result: str | None = None  # -> authorisation_result column (Tool Guard)
    trust_level: str | None = None           # -> overrides trust_level column
    risk_score: float | None = None          # -> risk_score column
    anomaly_score: float | None = None       # -> anomaly_score column
    metadata: dict = field(default_factory=dict)


@dataclass
class HookOutcome:
    """Combined result of every handler at one extension point, consumed by the agent."""

    decision: Decision
    payload: Any
    reason: str = ""
    guard_result: str | None = None
    authorisation_result: str | None = None
    trust_level: str | None = None
    risk_score: float | None = None
    anomaly_score: float | None = None
    hooks_ran: bool = False
    metadata: dict = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW


Handler = Callable[[Any, HookContext], Union[HookResult, None, Awaitable[Union[HookResult, None]]]]


class HookManager:
    def __init__(self, enabled: dict | None = None, review_behaviour: str = "block", on_error: str = "allow"):
        self.enabled: dict[str, bool] = {p: bool((enabled or {}).get(p, False)) for p in HOOK_POINTS}
        self.handlers: dict[str, list[Handler]] = {p: [] for p in HOOK_POINTS}
        self.review_behaviour = review_behaviour   # block | allow | human
        self.on_error = on_error
        self.plugins: list[str] = []
        # Set by the API server: async (point, payload, ctx, outcome) -> bool (approved).
        # Used when review_behaviour == "human"; without it REVIEW falls back to block.
        self.reviewer: Callable[[str, Any, HookContext, "HookOutcome"], Awaitable[bool]] | None = None

    # ---- registration -------------------------------------------------
    def register(self, point: str, handler: Handler) -> None:
        if point not in HOOK_POINTS:
            raise ValueError(f"unknown extension point {point!r}; expected one of {HOOK_POINTS}")
        self.handlers[point].append(handler)

    def clear(self, point: str | None = None) -> None:
        for p in [point] if point else HOOK_POINTS:
            self.handlers[p].clear()

    def set_enabled(self, point: str, value: bool) -> None:
        if point not in HOOK_POINTS:
            raise ValueError(point)
        self.enabled[point] = bool(value)

    def load_plugin(self, spec: str) -> None:
        """Load 'package.module:function' and call function(self)."""
        module_name, _, attr = spec.partition(":")
        module = importlib.import_module(module_name)
        getattr(module, attr or "register")(self)
        self.plugins.append(spec)
        log.info("loaded AgentShield plugin %s", spec)

    def defence_label(self) -> str:
        """Short description of the active security configuration, e.g. 'baseline' or
        'before_tool:scope_guard'. Stored with every run so baseline and defended runs can be compared."""
        active = [f"{p}:{'+'.join(getattr(h, '__name__', 'handler') for h in self.handlers[p])}"
                  for p in HOOK_POINTS if self.enabled[p] and self.handlers[p]]
        return ",".join(active) or "baseline"

    def status(self) -> dict:
        return {
            p: {"enabled": self.enabled[p], "handlers": [getattr(h, "__qualname__", repr(h)) for h in self.handlers[p]]}
            for p in HOOK_POINTS
        } | {"plugins": list(self.plugins), "review_behaviour": self.review_behaviour}

    # ---- dispatch ------------------------------------------------------
    async def dispatch(self, point: str, payload: Any, ctx: HookContext) -> HookOutcome:
        # Fast path: disabled or nothing registered -> pure pass-through (NFR-5).
        if not self.enabled.get(point) or not self.handlers[point]:
            return HookOutcome(decision=Decision.ALLOW, payload=payload)

        started = time.perf_counter()
        outcome = HookOutcome(decision=Decision.ALLOW, payload=payload, hooks_ran=True)
        reasons = []
        for handler in self.handlers[point]:
            try:
                result = handler(outcome.payload, ctx)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # a broken guard must not crash the agent
                log.exception("hook %s failed", point)
                outcome.metadata.setdefault("errors", []).append(f"{getattr(handler, '__qualname__', handler)}: {exc}")
                if self.on_error == "block":
                    outcome.decision = Decision.BLOCK
                    reasons.append(f"hook error: {exc}")
                    break
                continue
            if result is None:
                continue
            if result.payload is not None:
                outcome.payload = result.payload
            for name in ("guard_result", "authorisation_result", "trust_level", "anomaly_score"):
                if getattr(result, name) is not None:
                    setattr(outcome, name, getattr(result, name))
            if result.risk_score is not None:
                outcome.risk_score = max(outcome.risk_score or 0.0, result.risk_score)
            outcome.metadata.update(result.metadata or {})
            if result.reason:
                reasons.append(result.reason)
            decision = Decision(result.decision)
            if decision == Decision.BLOCK:
                outcome.decision = Decision.BLOCK
                break
            if decision == Decision.REVIEW:
                outcome.decision = Decision.REVIEW
        outcome.reason = "; ".join(reasons)
        # Time spent inside the security layer at this point (SRS Ch. 13 latency overhead).
        outcome.metadata["hook_ms"] = round((time.perf_counter() - started) * 1000, 3)
        if outcome.decision == Decision.REVIEW:
            outcome.metadata["original_decision"] = "REVIEW"
            if self.review_behaviour == "allow":
                outcome.decision = Decision.ALLOW
            elif self.review_behaviour == "human" and self.reviewer is not None:
                approved = await self.reviewer(point, outcome.payload, ctx, outcome)
                outcome.metadata["human_review"] = {"approved": approved}
                outcome.decision = Decision.ALLOW if approved else Decision.BLOCK
                outcome.reason = (outcome.reason + "; " if outcome.reason else "") + (
                    "approved by a human reviewer" if approved else "rejected by a human reviewer")
            elif self.review_behaviour == "human":
                outcome.metadata["human_review"] = {"approved": False, "note": "no reviewer attached (CLI run)"}
        return outcome


_manager: HookManager | None = None


def build_manager(cfg: dict) -> HookManager:
    hcfg = cfg.get("hooks", {}) or {}
    manager = HookManager(
        enabled=hcfg.get("enabled", {}),
        review_behaviour=hcfg.get("review_behaviour", "block"),
        on_error=hcfg.get("on_error", "allow"),
    )
    for spec in hcfg.get("plugins", []) or []:
        manager.load_plugin(spec)
    return manager


def get_manager() -> HookManager:
    global _manager
    if _manager is None:
        from .config import workspace_config

        _manager = build_manager(workspace_config())
    return _manager


def set_manager(manager: HookManager) -> None:
    global _manager
    _manager = manager


# ---- The four pass-through extension points (FR-18) -------------------------
async def before_input(payload: Any, ctx: HookContext, manager: HookManager | None = None) -> HookOutcome:
    ctx.point = "before_input"
    return await (manager or get_manager()).dispatch("before_input", payload, ctx)


async def before_tool(payload: Any, ctx: HookContext, manager: HookManager | None = None) -> HookOutcome:
    ctx.point = "before_tool"
    return await (manager or get_manager()).dispatch("before_tool", payload, ctx)


async def before_memory_write(payload: Any, ctx: HookContext, manager: HookManager | None = None) -> HookOutcome:
    ctx.point = "before_memory_write"
    return await (manager or get_manager()).dispatch("before_memory_write", payload, ctx)


async def before_output(payload: Any, ctx: HookContext, manager: HookManager | None = None) -> HookOutcome:
    ctx.point = "before_output"
    return await (manager or get_manager()).dispatch("before_output", payload, ctx)
