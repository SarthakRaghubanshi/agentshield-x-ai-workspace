"""EXAMPLE ONLY - shows how AgentShield-X attaches to the workspace. Not part of the workspace.

A deliberately tiny Tool Guard: it denies any tool call whose resource is not listed in the task's
`authorised.resources`. The real guards, policy engine and scoring are built by the security team.

Enable it without touching agent code:
    AGENTSHIELD_PLUGINS=examples.example_guard:register   (env var), or
    hooks.plugins: ["examples.example_guard:register"]  in config/workspace.yaml
and switch on the extension point:  hooks.enabled.before_tool: true
(the CLI flag  --plugin examples.example_guard:register  does both).
"""
from aiworkspace.hooks import Decision, HookContext, HookResult
from aiworkspace.tasks import in_scope


def scope_guard(payload: dict, ctx: HookContext) -> HookResult | None:
    authorised = (ctx.task.get("authorised") or {}).get("resources")
    if authorised is None:  # manual prompts carry no scope -> nothing to check
        return None
    resource = payload.get("resource")
    if in_scope(resource, authorised):
        return HookResult(authorisation_result="ALLOW", risk_score=0.0)
    return HookResult(
        decision=Decision.BLOCK,
        reason=f"resource {resource!r} is outside the task's authorised scope",
        authorisation_result="DENY",
        risk_score=0.9,
    )


def register(manager) -> None:
    manager.register("before_tool", scope_guard)
    manager.set_enabled("before_tool", True)
