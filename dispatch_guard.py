#!/usr/bin/env python3
"""
Claude Code PreToolUse hook for nagi-ledger: inspects an about-to-happen
Agent (subagent) dispatch against the ledger and escalates to a human
decision when the task is being over-retried or a known dead end / no-go
applies. Silent and frictionless otherwise.

Usage:
    python dispatch_guard.py    # wired to PreToolUse (matcher: Agent)

The Claude Code harness pipes the hook event as JSON on stdin.

Signalling contract:
  - allow  → exit 0, nothing on stdout, nothing on stderr
  - block  → exit 2, reason on stderr (stdout stays empty)
  - error  → exit 0 (fail-open), diagnostic on stderr

`exit 2` rather than a `permissionDecision: "ask"` payload: permission mode
`auto` silently swallows hook `ask` decisions (measured 2026-08-02), so the
guard would decide correctly and never be heard. Exit 2 is honored in every
permission mode. See emit_block() for details.

This script never writes to the ledger, and never fails closed on an
internal error — a broken guard must not be able to block all dispatches.

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


def emit_block(reason: str) -> None:
    """Signal a block to the harness: exit code 2 with the reason on stderr.

    NOT a `permissionDecision: "ask"` JSON payload. Measured 2026-08-02:
    under permission mode `auto` the harness silently swallows a hook's
    `ask` decision — the guard emitted a correct `ask` with its reason and
    the dispatch proceeded anyway, with the reason never surfacing to
    anyone. A guard that decides correctly and is never heard is worse
    than no guard. Exit code 2 is honored in every permission mode; it is
    the same mechanism the git guardrail hook relies on.
    """
    text = f"BLOCKED by dispatch_guard.\n{reason}"
    # Write UTF-8 bytes directly rather than print(): recorded reasons are
    # often Japanese, and on Windows sys.stderr's default encoding is the
    # console codepage (cp932 here), which cannot represent them — the text
    # would raise or arrive mojibake. json.dumps used to hide this by
    # escaping non-ASCII; plain text does not.
    buf = getattr(sys.stderr, "buffer", None)  # absent under pytest's capsys
    if buf is not None:
        try:
            buf.write((text + "\n").encode("utf-8", errors="replace"))
            buf.flush()
            return
        except Exception:
            pass
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


def run(event: dict) -> int:
    """Return the process exit code: 0 to allow, 2 to block."""
    tool_name = event.get("tool_name")
    tool_input = event.get("tool_input")

    if tool_name != "Agent":
        return 0
    if not isinstance(tool_input, dict):
        return 0

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
        return 0

    emit_block(build_reason(task, status, approaches))
    return 2


def main() -> int:
    event = _read_event()
    if not event:
        return 0
    try:
        return run(event)
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
