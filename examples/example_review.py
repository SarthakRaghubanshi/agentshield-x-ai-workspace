"""EXAMPLE ONLY - shows the human-review flow. Not part of the workspace.

Asks for a human decision before any email is sent. Run the console with
    AGENTSHIELD_PLUGINS=examples.example_review:register
choose "Ask me here (human review)" under *When a guard returns REVIEW*, then run a task that
sends email (e.g. "Meeting notes follow-up"). An Approve / Reject card appears in the live view.
"""
from aiworkspace.hooks import Decision, HookContext, HookResult


def review_outgoing_email(payload: dict, ctx: HookContext) -> HookResult | None:
    if payload.get("name") != "send_email":
        return None
    to = payload.get("args", {}).get("to", "?")
    return HookResult(decision=Decision.REVIEW, reason=f"outgoing email to {to}", authorisation_result="REVIEW")


def register(manager) -> None:
    manager.register("before_tool", review_outgoing_email)
    manager.set_enabled("before_tool", True)
