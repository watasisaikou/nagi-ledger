#!/usr/bin/env python3
"""
Claude Code hook entry point for nagi-ledger: auto-records subagent
dispatches and tool failures into the SQLite ledger without any model
involvement.

Usage:
    python hook_ingest.py agent-dispatch   # wired to PostToolUse (matcher: Agent)
    python hook_ingest.py tool-failure     # wired to PostToolUseFailure (all tools)

The Claude Code harness pipes the hook event as JSON on stdin. This script
must never block or break the harness: it always exits 0, catches every
exception, and prints nothing to stdout on success (stdout could be
interpreted by the harness). Debug info, if any, goes to stderr only.

Deliberately stdlib-only (json/sys/re + ledger, which is itself pure
sqlite3/os/pathlib) so it can run under ANY python interpreter that has
sqlite3 available — the venv python or the system python — without needing
`mcp` installed.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# hook cwd is not guaranteed to be this directory, so resolve the ledger
# import relative to this file rather than relying on cwd-based sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402  (import after sys.path fixup, intentional)

AUTO_PREFIX = "[auto] "


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _read_event() -> dict:
    """Read and parse the JSON event from stdin. Returns {} on any failure
    (empty stdin, invalid JSON, non-dict JSON)."""
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def handle_agent_dispatch(event: dict) -> None:
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")

    if tool_name != "Agent":
        return
    if not isinstance(tool_input, dict):
        return

    description = tool_input.get("description")
    prompt = tool_input.get("prompt")

    if isinstance(description, str) and description.strip():
        task = description.strip()
    elif isinstance(prompt, str) and prompt.strip():
        task = _collapse_whitespace(prompt)[:80]
    else:
        task = "unknown-dispatch"

    agent_type = tool_input.get("subagent_type") or "general-purpose"
    model = tool_input.get("model") or "inherit"

    if isinstance(prompt, str) and prompt.strip():
        brief_summary = _collapse_whitespace(prompt)[:200]
    else:
        brief_summary = "(no prompt)"
    brief_summary = AUTO_PREFIX + brief_summary

    conn = ledger.get_connection()
    try:
        dispatch_id, _retry_count = ledger.log_dispatch(
            conn, task, agent_type, model, brief_summary
        )
    finally:
        conn.close()

    print(f"dispatch_id={dispatch_id}", file=sys.stderr)


def handle_tool_failure(event: dict) -> None:
    tool_name = event.get("tool_name")
    tool_response = event.get("tool_response")

    if not isinstance(event, dict) or "tool_input" not in event:
        return

    if isinstance(tool_response, dict) and tool_response.get("error"):
        error_field = tool_response.get("error")
    elif tool_response is not None:
        error_field = tool_response
    else:
        error_field = "unknown error"

    error_text = error_field if isinstance(error_field, str) else json.dumps(error_field, default=str)
    short_error = _collapse_whitespace(error_text)[:200]

    tool_name_str = tool_name if isinstance(tool_name, str) and tool_name else "unknown-tool"
    description = f"{tool_name_str}: {short_error}"

    conn = ledger.get_connection()
    try:
        ledger.log_action(conn, tier=0, category="tool_failure", description=description, project=None)
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) < 2:
        return 0
    event_type = sys.argv[1]

    event = _read_event()
    if not event:
        return 0

    if event_type == "agent-dispatch":
        handle_agent_dispatch(event)
    elif event_type == "tool-failure":
        handle_tool_failure(event)
    # unknown event_type: silently no-op

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # never let the hook break the harness
        try:
            print(f"hook_ingest error: {exc}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(0)
