"""Human review queue for REVIEW decisions (hooks.review_behaviour: human).

When a security-layer handler returns Decision.REVIEW, the run pauses and the action appears in
the console with Approve / Reject buttons. No answer within `review_timeout_s` counts as a
rejection. This is plumbing only: deciding WHAT needs review is the security layer's job.
"""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

from .hooks import HookContext, HookOutcome
from .telemetry import Telemetry, now_iso


class ReviewQueue:
    def __init__(self, telemetry: Telemetry, timeout_s: float = 300.0):
        self.telemetry = telemetry
        self.timeout_s = timeout_s
        self.pending: dict[str, dict] = {}
        self._futures: dict[str, asyncio.Future] = {}

    async def request(self, point: str, payload: Any, ctx: HookContext, outcome: HookOutcome) -> bool:
        review_id = f"rev-{uuid.uuid4().hex[:8]}"
        item = {
            "review_id": review_id, "run_id": ctx.run_id, "task_id": ctx.task_id, "point": point,
            "step": ctx.step, "source": ctx.source, "reason": outcome.reason,
            "payload": payload if isinstance(payload, (str, dict, list)) else str(payload),
            "created_at": now_iso(), "timeout_s": self.timeout_s,
        }
        future = asyncio.get_running_loop().create_future()
        self.pending[review_id] = item
        self._futures[review_id] = future
        self.telemetry._publish(ctx.run_id, {"kind": "review", "review": json.loads(json.dumps(item, default=str))})
        try:
            approved = await asyncio.wait_for(future, timeout=self.timeout_s)
        except asyncio.TimeoutError:
            approved = False
        finally:
            self.pending.pop(review_id, None)
            self._futures.pop(review_id, None)
        self.telemetry._publish(ctx.run_id, {"kind": "review_done", "review_id": review_id, "approved": approved})
        return approved

    def resolve(self, review_id: str, approved: bool) -> bool:
        future = self._futures.get(review_id)
        if future is None or future.done():
            return False
        future.set_result(bool(approved))
        return True

    def list(self) -> list[dict]:
        return list(self.pending.values())
