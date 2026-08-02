#!/usr/bin/env python3
"""
nagi-ledger MCP server.

An autonomous-development audit ledger. Records agent dispatches, verification
verdicts, retry counts, and autonomous actions into SQLite, and exposes query
tools so an AI agent can introspect its own autonomous-dev loop and enforce
budget limits (e.g. "don't retry the same task more than twice").

Storage: SQLite at %USERPROFILE%\\.nagi\\ledger.db by default, overridable via
the NAGI_LEDGER_DB environment variable (see ledger.get_db_path).

Run directly to serve over stdio:
    python server.py
"""

from __future__ import annotations

from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

import ledger

mcp = FastMCP("nagi_ledger_mcp")


@mcp.tool(
    name="ledger_log_action",
    annotations={
        "title": "Log Autonomous Action",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def ledger_log_action(
    tier: int,
    category: str,
    description: str,
    project: Optional[str] = None,
) -> dict[str, Any]:
    """Record a single autonomous action taken during a self-directed dev loop.

    Use this to log any action the agent takes on its own initiative, tagged
    with a tier indicating how consequential it was.

    Args:
        tier (int): Impact tier. Must be 0 (routine/info), 1 (notable), or 2
            (high-impact, e.g. irreversible or user-facing).
        category (str): Short category label, e.g. "refactor", "deploy",
            "file_write", "dependency_change". Must be non-empty.
        description (str): Human-readable description of what was done.
            Must be non-empty.
        project (Optional[str]): Project/repo name this action belongs to.

    Returns:
        dict: {"id": int} — the new action's row id.

    Errors:
        Raises ValueError if tier is not in {0,1,2} or category/description
        are empty, with a message explaining the valid values.
    """
    conn = ledger.get_connection()
    try:
        new_id = ledger.log_action(conn, tier, category, description, project)
        return {"id": new_id}
    finally:
        conn.close()


@mcp.tool(
    name="ledger_log_dispatch",
    annotations={
        "title": "Log Subagent Dispatch",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    },
)
def ledger_log_dispatch(
    task: str,
    agent_type: str,
    model: str,
    brief_summary: str,
) -> dict[str, Any]:
    """Record a subagent dispatch (e.g. via the Agent tool) for audit and retry tracking.

    Call this every time a subagent is dispatched for a task, so retries of
    the same task can be counted and budget-limited via ledger_task_status.

    Args:
        task (str): Stable identifier/description of the task being dispatched.
            Use the SAME string across retries of the same underlying task so
            retry counting works.
        agent_type (str): Subagent type used (e.g. "fork", "general-purpose").
        model (str): Model used for the dispatch (e.g. "sonnet", "opus").
        brief_summary (str): One-line summary of what the dispatch was asked to do.

    Returns:
        dict: {"id": int, "retry_count": int} where retry_count is the number
        of PRIOR dispatches recorded under the same task (0 for the first).

    Errors:
        Raises ValueError if any field is empty.
    """
    conn = ledger.get_connection()
    try:
        new_id, retry_count = ledger.log_dispatch(conn, task, agent_type, model, brief_summary)
        return {"id": new_id, "retry_count": retry_count}
    finally:
        conn.close()


@mcp.tool(
    name="ledger_log_verdict",
    annotations={
        "title": "Attach Verdict to Dispatch",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def ledger_log_verdict(
    dispatch_id: int,
    verdict: str,
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """Attach a verification verdict to a previously logged dispatch.

    Use this after independently verifying a subagent's work, to record
    whether its claims were confirmed, refuted, or partially true.

    Args:
        dispatch_id (int): The id returned by ledger_log_dispatch for the
            dispatch being verified.
        verdict (str): One of "CONFIRMED", "REFUTED", "PARTIAL".
        notes (Optional[str]): Optional free-text notes on the verification.

    Returns:
        dict: {"ok": true, "task": str} — the task string of the dispatch
        the verdict was attached to.

    Errors:
        Raises ValueError with an actionable message if dispatch_id does not
        exist, or if verdict is not one of the three allowed values.
    """
    conn = ledger.get_connection()
    try:
        task = ledger.log_verdict(conn, dispatch_id, verdict, notes)
        return {"ok": True, "task": task}
    finally:
        conn.close()


@mcp.tool(
    name="ledger_task_status",
    annotations={
        "title": "Get Task Retry/Verdict Status",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def ledger_task_status(task: str) -> dict[str, Any]:
    """Look up dispatch/retry/verdict status for a given task string.

    Use this BEFORE dispatching a subagent to check whether the same task
    has already been retried too many times (budget enforcement).

    Args:
        task (str): The exact task string used with ledger_log_dispatch.

    Returns:
        dict: {
            "dispatch_count": int,        # total dispatches recorded for this task
            "retry_count": int,           # dispatch_count - 1, floored at 0
            "last_verdict": str | None,   # verdict of most recent dispatch, or null
            "over_retry_limit": bool      # true when retry_count >= 2
        }
    """
    conn = ledger.get_connection()
    try:
        return ledger.task_status(conn, task)
    finally:
        conn.close()


@mcp.tool(
    name="ledger_session_report",
    annotations={
        "title": "Session Report (Markdown)",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def ledger_session_report(since_hours: int = 24) -> str:
    """Generate a markdown report of actions and dispatches from the last N hours.

    Actions are grouped by category; dispatches are listed with their
    resolved verdict or PENDING if not yet verified. Useful for a
    human-readable audit summary of recent autonomous activity.

    Args:
        since_hours (int): How many hours back to include. Defaults to 24.

    Returns:
        str: Markdown report, or a short "no entries" message if nothing
        was logged in the window.
    """
    conn = ledger.get_connection()
    try:
        return ledger.session_report(conn, since_hours)
    finally:
        conn.close()


@mcp.tool(
    name="ledger_stats",
    annotations={
        "title": "Ledger Stats",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    },
)
def ledger_stats(days: int = 7) -> dict[str, Any]:
    """Aggregate statistics over actions and dispatches from the last N days.

    Args:
        days (int): How many days back to include. Defaults to 7.

    Returns:
        dict: {
            "actions_by_tier": {"0": int, "1": int, "2": int},
            "actions_by_category": {category: int, ...},
            "dispatch_count": int,
            "verdicts": {"CONFIRMED": int, "REFUTED": int, "PARTIAL": int, "PENDING": int}
        }
    """
    conn = ledger.get_connection()
    try:
        return ledger.stats(conn, days)
    finally:
        conn.close()


if __name__ == "__main__":
    mcp.run()
