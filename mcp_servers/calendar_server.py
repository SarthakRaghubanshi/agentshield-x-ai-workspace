"""Sample MCP server: a team calendar stored in the sandbox (SANDBOX_ROOT/calendar/events.json).

It runs as a separate process over stdio, exactly like any third-party MCP server would. Add more
servers in config/mcp_servers.yaml - the workspace picks them up without code changes.
"""
import json
import os
from pathlib import Path

from mcp.server.mcpserver import MCPServer

ROOT = Path(os.environ.get("SANDBOX_ROOT", "runtime/sandbox"))
EVENTS = ROOT / "calendar" / "events.json"

server = MCPServer("calendar", instructions="Sample team calendar for the AgentShield-X sandbox.")


def _load() -> list[dict]:
    return json.loads(EVENTS.read_text(encoding="utf-8")) if EVENTS.exists() else []


@server.tool()
def list_events(date: str = "") -> str:
    """List calendar events, optionally only those on a given date (YYYY-MM-DD)."""
    events = [e for e in _load() if not date or e["date"] == date]
    if not events:
        return "No events."
    return "\n".join(f"{e['id']} | {e['date']} {e['time']} | {e['title']} | {', '.join(e['attendees'])}" for e in events)


@server.tool()
def create_event(title: str, date: str, time: str, attendees: list[str] | None = None) -> str:
    """Create a calendar event. date is YYYY-MM-DD, time is HH:MM."""
    events = _load()
    event = {"id": f"evt-{len(events) + 1}", "title": title, "date": date, "time": time, "attendees": attendees or []}
    events.append(event)
    EVENTS.parent.mkdir(parents=True, exist_ok=True)
    EVENTS.write_text(json.dumps(events, indent=2), encoding="utf-8")
    return f"Created {event['id']}: {title} on {date} at {time}."


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Sample calendar MCP server")
    parser.add_argument("--http", type=int, metavar="PORT", help="serve Streamable HTTP on PORT instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    opts = parser.parse_args()
    if opts.http:
        server.run("streamable-http", host=opts.host, port=opts.http)   # url: http://HOST:PORT/mcp
    else:
        server.run("stdio")
