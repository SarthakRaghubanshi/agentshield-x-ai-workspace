"""Model abstraction (PRD FR-1..FR-5): every model call goes through LiteLLM via ModelClient.

ModelClient.complete() is the single place where the agent talks to an LLM, so it is also the
future interception point for the Input/Output Guards at the model boundary.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

# Use LiteLLM's bundled model cost map instead of downloading it (offline sandbox, fast start).
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

import litellm  # noqa: E402

from .config import model_registry, workspace_config

litellm.drop_params = True          # ignore params (e.g. seed) a provider does not support
litellm.suppress_debug_info = True


# ---- client -------------------------------------------------------------------
@dataclass
class ModelReply:
    content: str | None
    tool_calls: list[dict]                 # [{"id", "name", "arguments": dict, "raw_arguments": str}]
    raw_message: dict
    model: str
    latency_ms: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    extra: dict = field(default_factory=dict)


def resolve_model(model_id: str) -> dict:
    """Look up a model id in config/models.yaml; unknown ids are treated as raw LiteLLM strings."""
    for entry in model_registry():
        if entry["id"] == model_id or entry["model"] == model_id:
            return entry
    return {"id": model_id, "model": model_id, "label": model_id, "kind": "custom"}


_PROBE_CACHE: dict[str, tuple[float, object]] = {}   # url -> (checked_at, response json or None)
_PROBE_TTL_S = 15.0


async def _probe(http, url: str) -> object:
    """GET a local model endpoint once; None if unreachable. Hard 1 s cap per request, cached
    briefly (a closed localhost port can take seconds to refuse on Windows)."""
    cached = _PROBE_CACHE.get(url)
    if cached and time.monotonic() - cached[0] < _PROBE_TTL_S:
        return cached[1]
    try:
        r = await asyncio.wait_for(http.get(url), timeout=1.0)
        result = r.json() if r.status_code == 200 else None
    except Exception:
        result = None
    _PROBE_CACHE[url] = (time.monotonic(), result)
    return result


def _probe_url(entry: dict) -> str | None:
    # "localhost" can resolve to IPv6 first on Windows, adding a slow fallback for IPv4-only servers.
    base = (entry.get("api_base") or "").rstrip("/").replace("://localhost", "://127.0.0.1")
    if not base:
        return None
    return f"{base}/api/tags" if entry["model"].startswith("ollama") else f"{base}/models"


def _endpoint_up(entry: dict, response: object) -> bool:
    """A local model is usable if its endpoint answered (and, for Ollama, the model is pulled)."""
    if response is None:
        return False
    if entry["model"].startswith("ollama"):
        name = entry["model"].split("/", 1)[1]
        pulled = {m.get("name") for m in (response.get("models", []) if isinstance(response, dict) else [])}
        return name in pulled or f"{name}:latest" in pulled
    return True


async def list_models() -> list[dict]:
    """Registered models with an `available` flag (API key present / local endpoint reachable)."""
    entries = model_registry()
    urls = sorted({u for m in entries if m.get("kind") == "local" and (u := _probe_url(m))})
    fresh = {u: c[1] for u in urls if (c := _PROBE_CACHE.get(u)) and time.monotonic() - c[0] < _PROBE_TTL_S}
    responses = dict(fresh)
    if len(fresh) < len(urls):
        import httpx

        stale = [u for u in urls if u not in fresh]
        async with httpx.AsyncClient(timeout=1.0) as http:  # one client: building SSL contexts is slow
            responses.update(zip(stale, await asyncio.gather(*[_probe(http, u) for u in stale])))  # each endpoint once
    local_up = [_endpoint_up(m, responses.get(_probe_url(m))) if m.get("kind") == "local" else False for m in entries]
    out = []
    for m, up in zip(entries, local_up):
        if m.get("kind") == "local":
            available, why = up, "" if up else f"endpoint {m.get('api_base')} not reachable or model not pulled"
        else:
            env = m.get("requires_env")
            available, why = (not env or bool(os.environ.get(env))), ("" if not env or os.environ.get(env) else f"set {env}")
        out.append({k: v for k, v in m.items() if k != "api_key"} | {"available": available, "unavailable_reason": why})
    return out


class ModelClient:
    def __init__(self, cfg: dict | None = None):
        cfg = cfg or workspace_config()
        self.temperature = cfg.get("agent", {}).get("temperature", 0.0)
        self.seed = cfg.get("agent", {}).get("seed")
        budget = (cfg.get("budget") or {}).get("max_usd")
        if budget:
            litellm.max_budget = float(budget)

    async def complete(self, model_id: str, messages: list[dict], tools: list[dict] | None = None) -> ModelReply:
        entry = resolve_model(model_id)
        kwargs: dict[str, Any] = {"model": entry["model"], "messages": messages, "temperature": self.temperature}
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if self.seed is not None:
            kwargs["seed"] = self.seed
        for key in ("api_base", "api_key"):
            if entry.get(key):
                kwargs[key] = entry[key]

        start = time.perf_counter()
        response = await litellm.acompletion(**kwargs)
        latency = (time.perf_counter() - start) * 1000

        msg = response.choices[0].message
        tool_calls = []
        for tc in getattr(msg, "tool_calls", None) or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except json.JSONDecodeError:
                args = {"_unparsed": raw}
            tool_calls.append({"id": tc.id or f"call_{uuid.uuid4().hex[:8]}", "name": tc.function.name,
                               "arguments": args, "raw_arguments": raw if isinstance(raw, str) else json.dumps(raw)})
        usage = getattr(response, "usage", None)
        try:
            cost = float(litellm.completion_cost(completion_response=response))
        except Exception:
            cost = 0.0
        raw_message = {"role": "assistant", "content": msg.content}
        if tool_calls:
            raw_message["tool_calls"] = [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["raw_arguments"]}}
                for c in tool_calls
            ]
        return ModelReply(
            content=msg.content, tool_calls=tool_calls, raw_message=raw_message, model=entry["model"],
            latency_ms=round(latency, 2),
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            total_tokens=getattr(usage, "total_tokens", 0) or 0,
            cost_usd=cost,
        )
