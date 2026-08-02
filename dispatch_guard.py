#!/usr/bin/env python3
"""
Claude Code PreToolUse hook for nagi-ledger: inspects an about-to-happen
Agent (subagent) dispatch against the ledger and escalates to a human
decision when the task is being over-retried or a known dead end / no-go
applies. Silent and frictionless otherwise.

Usage:
    python dispatch_guard.py    # wired to PreToolUse (matcher: Agent)

The Claude Code harness pipes the hook event as JSON on stdin. On escalation
this script prints a single-line JSON decision to stdout (see the
`hookSpecificOutput` contract below); otherwise it prints nothing. This
script must never block or break the harness: it always exits 0, catches
every exception, and never writes to the ledger.

Deliberately stdlib-only (json/re/sys + ledger, which is itself pure
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

RETRY_LIMIT = 2  # same-purpose retries are limited to this many

MAX_REASON_LEN = 150
MAX_ITEMS_PER_BUCKET = 5
MAX_TOTAL_LEN = 1200


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate(text: str, limit: int) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


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


def derive_task(tool_input: dict) -> str:
    """Derive the task string from an Agent tool_input dict.

    Must match hook_ingest.py's derivation rules exactly, since task
    strings are the join key between dispatch logging and this guard:
    `description` if non-empty, else first 80 chars of whitespace-collapsed
    `prompt`, else "unknown-dispatch".
    """
    description = tool_input.get("description")
    prompt = tool_input.get("prompt")

    if isinstance(description, str) and description.strip():
        return description.strip()
    if isinstance(prompt, str) and prompt.strip():
        return _collapse_whitespace(prompt)[:80]
    return "unknown-dispatch"


def _format_bucket(label: str, items: list[dict], budget: int) -> list[str]:
    lines = []
    shown = items[:MAX_ITEMS_PER_BUCKET]
    for item in shown:
        approach = _truncate(item.get("approach", ""), MAX_REASON_LEN)
        reason = _truncate(item.get("reason", ""), MAX_REASON_LEN)
        lines.append(f"- [{label}] {approach} — {reason}")
    remaining = len(items) - len(shown)
    if remaining > 0:
        lines.append(f"  (+{remaining} more {label})")
    return lines


def build_reason(task: str, status: dict, approaches: dict) -> str:
    parts: list[str] = []
    parts.append(f"Task: {_truncate(task, 100)}")

    if status.get("over_retry_limit"):
        last_verdict = status.get("last_verdict") or "PENDING"
        parts.append(
            f"prior dispatches: {status.get('dispatch_count', 0)}, "
            f"last verdict: {last_verdict}"
        )
        parts.append("Budget rule: same-purpose retries are limited to 2.")

    dead_ends = approaches.get("dead_ends") or []
    no_gos = approaches.get("no_gos") or []

    if dead_ends:
        parts.extend(_format_bucket("DEAD_END", dead_ends, MAX_REASON_LEN))
    if no_gos:
        parts.extend(_format_bucket("NO_GO", no_gos, MAX_REASON_LEN))

    parts.append(
        "Proceed only if you have new information that invalidates the "
        "above; otherwise change approach or stop."
    )

    reason = "\n".join(parts)
    if len(reason) > MAX_TOTAL_LEN:
        reason = reason[: MAX_TOTAL_LEN - 1] + "…"
    return reason


def emit_ask(reason: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))


def run(event: dict) -> None:
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")

    if tool_name != "Agent":
        return
    if not isinstance(tool_input, dict):
        return

    task = derive_task(tool_input)

    conn = ledger.get_connection()
    try:
        status = ledger.task_status(conn, task)
        approaches = ledger.check_approaches(conn, task)
    finally:
        conn.close()

    should_escalate = (
        status.get("retry_count", 0) >= RETRY_LIMIT
        or bool(approaches.get("dead_ends"))
        or bool(approaches.get("no_gos"))
    )
    if not should_escalate:
        return

    reason = build_reason(task, status, approaches)
    emit_ask(reason)


def main() -> int:
    event = _read_event()
    if not event:
        return 0
    try:
        run(event)
    except Exception as exc:  # fail-open: a broken guard must never block a dispatch
        try:
            print(f"dispatch_guard error: {exc}", file=sys.stderr)
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # belt-and-suspenders: fail-open no matter what
        try:
            print(f"dispatch_guard error: {exc}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(0)
