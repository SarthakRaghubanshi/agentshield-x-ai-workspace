"""Command line: run tasks / batches, export telemetry, start the server.

    python -m aiworkspace serve                                   # UI + API on http://localhost:8000
    python -m aiworkspace tasks                                   # list tasks
    python -m aiworkspace chat --model ollama-qwen3-4b            # interactive conversation
    python -m aiworkspace run --task summarise_report --model ollama-qwen2.5
    python -m aiworkspace run --prompt "What is 2+2? Use the calculator." --model gemini-flash
    python -m aiworkspace batch --label attack --model ollama-qwen2.5 --repeats 3
    python -m aiworkspace batch --plugin examples.example_guard:register      # attach a guard
    python -m aiworkspace export --fmt csv --table events --out runtime/events.csv
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import workspace_config


def _workspace(args):
    from .workspace import Workspace

    cfg = workspace_config()
    ws = Workspace(cfg)
    for spec in getattr(args, "plugin", None) or []:
        ws.hooks.load_plugin(spec)
    for point in getattr(args, "enable", None) or []:
        ws.hooks.set_enabled(point, True)
    return ws


def _print_run(run: dict) -> None:
    flag = {None: "n/a", 1: "yes", 0: "no", True: "yes", False: "no"}
    print(f"{run['run_id']}  {run['task_id']:<28} {run['status']:<10} task_success={flag[run['task_success']]:<4} "
          f"attack_success={flag[run['attack_success']]:<4} steps={run['steps']}")


async def _run(args) -> int:
    ws = _workspace(args)
    await ws.start()
    try:
        for i in range(args.repeats):
            run = await ws.run(task_id=args.task, prompt=args.prompt, model=args.model, repeat_index=i)
            _print_run(run)
            if args.verbose:
                print("\n--- final output ---\n" + (run["final_output"] or ""))
    finally:
        await ws.stop()
    return 0


async def _chat(args) -> int:
    """Interactive conversation with the agent (short-term memory across turns)."""
    ws = _workspace(args)
    await ws.start()
    session_id = args.session
    print(f"Chatting with {args.model or ws.cfg['agent']['default_model']}. Empty line or Ctrl+C to quit.")
    try:
        while True:
            try:
                prompt = input("\nyou> ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not prompt:
                break
            run = await ws.run(prompt=prompt, model=args.model, session_id=session_id)
            session_id = run["session_id"]
            print(f"\nagent> {run['final_output']}\n   [{run['run_id']} - {run['status']} - {run['steps']} steps]")
    finally:
        await ws.stop()
    if session_id:
        print(f"\nsession: {session_id}  (resume with --session {session_id})")
    return 0


async def _batch(args) -> int:
    ws = _workspace(args)
    await ws.start()
    try:
        ids = args.tasks or [t["id"] for t in ws.tasks.values() if not args.label or t["label"] == args.label]
        result = await ws.run_batch(ids, model=args.model, repeats=args.repeats)
        for run in result["runs"]:
            if "run_id" in run:
                _print_run(run)
            else:
                print("ERROR", run)
        print(f"\nbatch {result['batch_id']} summary:")
        pct = lambda v: "n/a" if v is None else f"{v:.0%}"
        for g in result["summary"]["groups"]:
            print(f"  {g['model']:<16} {g['defence'][:24]:<24} {g['label']:<7} {g['category']:<19} runs={g['runs']:<3} "
                  f"task_ok={pct(g['task_success_rate']):<5} ASR={pct(g['attack_success_rate']):<5} "
                  f"unsafe={pct(g['unsafe_action_rate']):<5} misuse={pct(g['tool_misuse_rate']):<5} "
                  f"false_block={pct(g['false_blocking_rate'])}")
    finally:
        await ws.stop()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="aiworkspace", description="AgentShield-X AI Workspace")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="start the API + UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)

    sub.add_parser("tasks", help="list task files")

    c = sub.add_parser("chat", help="interactive conversation with the agent")
    c.add_argument("--model", default=None)
    c.add_argument("--session", default=None, help="continue an earlier session id")
    c.add_argument("--plugin", action="append")
    c.add_argument("--enable", action="append", choices=["before_input", "before_tool", "before_memory_write", "before_output"])

    for name in ("run", "batch"):
        r = sub.add_parser(name)
        r.add_argument("--model", default=None, help="model id from config/models.yaml or a raw LiteLLM string")
        r.add_argument("--repeats", type=int, default=1)
        r.add_argument("--plugin", action="append", help="load a security-layer plugin 'module:function'")
        r.add_argument("--enable", action="append", choices=["before_input", "before_tool", "before_memory_write", "before_output"])
        if name == "run":
            g = r.add_mutually_exclusive_group(required=True)
            g.add_argument("--task")
            g.add_argument("--prompt")
            r.add_argument("-v", "--verbose", action="store_true")
        else:
            r.add_argument("--tasks", nargs="*", help="task ids (default: all)")
            r.add_argument("--label", choices=["benign", "attack"])

    e = sub.add_parser("export", help="export telemetry as CSV/JSON (FR-23)")
    e.add_argument("--fmt", choices=["csv", "json"], default="csv")
    e.add_argument("--table", choices=["events", "runs"], default="events")
    e.add_argument("--run-id")
    e.add_argument("--out")

    args = p.parse_args(argv)
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("aiworkspace.api:app", host=args.host, port=args.port)
        return 0
    if args.cmd == "tasks":
        from .tasks import load_tasks

        for t in load_tasks(workspace_config().get("tasks_dir", "tasks")).values():
            print(f"{t['id']:<28} {t['label']:<7} {t['category']:<20} {t['name']}")
        return 0
    if args.cmd == "run":
        return asyncio.run(_run(args))
    if args.cmd == "chat":
        return asyncio.run(_chat(args))
    if args.cmd == "batch":
        return asyncio.run(_batch(args))
    if args.cmd == "export":
        from .config import resolve_path
        from .telemetry import Telemetry

        tel = Telemetry(resolve_path(workspace_config().get("telemetry", {}).get("db_path", "runtime/telemetry.db")))
        data = tel.export(args.fmt, args.table, args.run_id)
        if args.out:
            Path(args.out).write_text(data, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            sys.stdout.write(data)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
