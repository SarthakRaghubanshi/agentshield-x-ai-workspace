"""Per-run context shared by the agent loop, the tool gateway and memory."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .hooks import HookContext, HookManager, HookOutcome
from .telemetry import Telemetry


@dataclass
class RunContext:
    run_id: str
    task_id: str
    agent_id: str
    model: str
    telemetry: Telemetry
    hooks: HookManager
    trust_levels: dict
    label: str | None = None
    category: str | None = None
    task: dict = field(default_factory=dict)
    step: int = 0
    turn: int = 1
    memory: Any = None   # the LongTermMemory this run reads/writes (chat vs experiments)
    session_id: str | None = None
    trace: list = field(default_factory=list)

    def trust(self, source: str) -> str:
        return self.trust_levels.get(source, "unknown")

    def hook_ctx(self, source: str, tool: dict | None = None, extra: dict | None = None) -> HookContext:
        return HookContext(
            point="", run_id=self.run_id, task_id=self.task_id, agent_id=self.agent_id, step=self.step,
            source=source, trust_level=self.trust(source), model=self.model, task=self.task,
            tool=tool, trace=list(self.trace), extra={"turn": self.turn, "session_id": self.session_id} | (extra or {}),
        )

    def log(self, event_type: str, hook: HookOutcome | None = None, guard_column: str | None = None, **fields: Any) -> dict:
        """Write one telemetry event, filling identifiers and any security-layer verdicts."""
        base = dict(run_id=self.run_id, task_id=self.task_id, agent_id=self.agent_id, step=self.step,
                    turn=self.turn, label=self.label, category=self.category, model=self.model)
        overrides: dict[str, Any] = {}
        if hook is not None and hook.hooks_ran:
            if guard_column and hook.guard_result is not None:
                overrides[guard_column] = hook.guard_result
            if hook.authorisation_result is not None:
                overrides["authorisation_result"] = hook.authorisation_result
            if hook.trust_level is not None:
                overrides["trust_level"] = hook.trust_level
            overrides["risk_score"] = hook.risk_score
            overrides["anomaly_score"] = hook.anomaly_score
            overrides["final_decision"] = hook.decision.value
            details = dict(fields.pop("details", None) or {})
            details["hook"] = {"reason": hook.reason, **hook.metadata}
            fields["details"] = details
        row = self.telemetry.log(event_type, **(base | fields | overrides))
        self.trace.append({k: row.get(k) for k in ("event_type", "tool_called", "resource_accessed", "provenance_source", "final_decision", "step")})
        return row
