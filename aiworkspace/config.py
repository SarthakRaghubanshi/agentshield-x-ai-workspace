"""Configuration loading: YAML files under config/ plus environment variables (.env supported)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

ROOT = Path(os.environ.get("AIWORKSPACE_ROOT", Path(__file__).resolve().parent.parent))
CONFIG_DIR = Path(os.environ.get("AIWORKSPACE_CONFIG_DIR", ROOT / "config"))

if load_dotenv:
    load_dotenv(ROOT / ".env", override=False)

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Expand ${VAR} and ${VAR:-default} inside strings (recursively)."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def _load_yaml(name: str) -> dict:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def deep_merge(base: dict, override: dict | None) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


@lru_cache(maxsize=1)
def workspace_config() -> dict:
    cfg = expand_env(_load_yaml("workspace.yaml"))
    # Environment overrides that matter inside Docker.
    if os.environ.get("SANDBOX_MODE"):
        cfg.setdefault("sandbox", {})["mode"] = os.environ["SANDBOX_MODE"]
    if os.environ.get("SANDBOX_URL"):
        cfg.setdefault("sandbox", {})["url"] = os.environ["SANDBOX_URL"]
    if os.environ.get("AGENTSHIELD_PLUGINS"):
        plugins = [p.strip() for p in os.environ["AGENTSHIELD_PLUGINS"].split(",") if p.strip()]
        cfg.setdefault("hooks", {})["plugins"] = plugins
    return cfg


@lru_cache(maxsize=1)
def model_registry() -> list[dict]:
    return expand_env(_load_yaml("models.yaml").get("models", []))


def mcp_config(path: str | None = None) -> dict:
    p = Path(path) if path else CONFIG_DIR / "mcp_servers.yaml"
    if not p.is_absolute():
        p = ROOT / p
    if not p.exists():
        return {}
    with open(p, encoding="utf-8") as f:
        return expand_env(yaml.safe_load(f) or {})


def resolve_path(p: str | Path) -> Path:
    p = Path(p)
    return p if p.is_absolute() else ROOT / p


def config_hash(cfg: dict) -> str:
    """Short stable hash of the effective config, stored with each run (NFR-3 reproducibility)."""
    blob = json.dumps(cfg, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()[:12]
