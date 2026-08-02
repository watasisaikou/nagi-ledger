"""
Pure logic layer for nagi-ledger: an autonomous-development audit ledger.

This module contains no MCP-specific code. All functions operate on a
sqlite3.Connection and are directly unit-testable. server.py wraps these
functions as MCP tools.

Schema:
    actions     — a single autonomous action taken by an agent (tiered).
    dispatches  — a subagent dispatch, optionally later annotated with a verdict.
    approaches  — a recorded outcome (dead end / no-go / works) for a task,
                  so future attempts can check before retrying.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

VALID_TIERS = (0, 1, 2)
VALID_VERDICTS = ("CONFIRMED", "REFUTED", "PARTIAL")
VALID_OUTCOMES = ("DEAD_END", "NO_GO", "WORKS")

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  tier INTEGER NOT NULL CHECK(tier IN (0,1,2)),
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  project TEXT
);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  model TEXT NOT NULL,
  brief_summary TEXT NOT NULL,
  verdict TEXT CHECK(verdict IN ('CONFIRMED','REFUTED','PARTIAL') OR verdict IS NULL),
  verdict_ts TEXT,
  verdict_notes TEXT
);
CREATE TABLE IF NOT EXISTS approaches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task TEXT NOT NULL,
  approach TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('DEAD_END','NO_GO','WORKS')),
  reason TEXT NOT NULL
);
"""


def get_db_path() -> Path:
    """Resolve the ledger DB path.

    Single source of truth for the DB location: reads the NAGI_LEDGER_DB
    environment variable if set (used by tests / callers who want an
    isolated DB), otherwise defaults to ~/.nagi/ledger.db.
    """
    override = os.environ.get("NAGI_LEDGER_DB")
    if override:
        return Path(override)
    return Path.home() / ".nagi" / "ledger.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a sqlite3 connection to the ledger DB, creating the parent dir
    and schema if needed. Caller is responsible for closing the connection.
    """
    path = db_path if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Async hooks can fire concurrently; WAL + busy_timeout prevent
    # "database is locked" from silently dropping a write.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they do not already exist."""
    conn.executescript(SCHEMA)
    conn.commit()


def _require_nonempty(value: str, field_name: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"'{field_name}' must be a non-empty string.")
    return str(value).strip()


def log_action(
    conn: sqlite3.Connection,
    tier: int,
    category: str,
    description: str,
    project: str | None = None,
) -> int:
    """Insert an autonomous action record. Returns the new row id.

    Raises ValueError with an actionable message if tier is not in {0,1,2}
    or if category/description are empty.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"Invalid tier {tier!r}: must be one of {VALID_TIERS} "
            "(0=info, 1=notable, 2=high-impact)."
        )
    category = _require_nonempty(category, "category")
    description = _require_nonempty(description, "description")
    project = str(project).strip() if project else None

    cur = conn.execute(
        "INSERT INTO actions (tier, category, description, project) VALUES (?, ?, ?, ?)",
        (tier, category, description, project),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_dispatch(
    conn: sqlite3.Connection,
    task: str,
    agent_type: str,
    model: str,
    brief_summary: str,
) -> tuple[int, int]:
    """Insert a subagent dispatch record.

    Returns (id, retry_count) where retry_count is the number of PRIOR
    dispatches recorded for the same `task` string (0 for the first
    dispatch of a given task).

    Raises ValueError if any required field is empty.
    """
    task = _require_nonempty(task, "task")
    agent_type = _require_nonempty(agent_type, "agent_type")
    model = _require_nonempty(model, "model")
    brief_summary = _require_nonempty(brief_summary, "brief_summary")

    prior_count = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE task = ?", (task,)
    ).fetchone()["n"]

    cur = conn.execute(
        "INSERT INTO dispatches (task, agent_type, model, brief_summary) VALUES (?, ?, ?, ?)",
        (task, agent_type, model, brief_summary),
    )
    conn.commit()
    return int(cur.lastrowid), int(prior_count)


def log_verdict(
    conn: sqlite3.Connection,
    dispatch_id: int,
    verdict: str,
    notes: str | None = None,
) -> str:
    """Attach a verdict to an existing dispatch. Returns the dispatch's task string.

    Raises ValueError (with an actionable message) if:
      - dispatch_id does not exist
      - verdict is not one of CONFIRMED/REFUTED/PARTIAL
    """
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict {verdict!r}: must be one of {VALID_VERDICTS}.")

    row = conn.execute("SELECT task FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    if row is None:
        raise ValueError(
            f"No dispatch found with id={dispatch_id}. "
            "Call ledger_log_dispatch first, or check the id via ledger_task_status."
        )

    conn.execute(
        "UPDATE dispatches SET verdict = ?, verdict_ts = datetime('now'), verdict_notes = ? "
        "WHERE id = ?",
        (verdict, notes, dispatch_id),
    )
    conn.commit()
    return row["task"]


def task_status(conn: sqlite3.Connection, task: str) -> dict:
    """Return retry/verdict status for a given task string.

    {
      "dispatch_count": int,
      "retry_count": int,          # dispatch_count - 1, floored at 0
      "last_verdict": str | None,  # verdict of the most recent dispatch, or None
      "over_retry_limit": bool     # True when retry_count >= 2
    }
    """
    task = _require_nonempty(task, "task")

    rows = conn.execute(
        "SELECT verdict FROM dispatches WHERE task = ? ORDER BY id DESC", (task,)
    ).fetchall()
    dispatch_count = len(rows)
    retry_count = max(dispatch_count - 1, 0)
    last_verdict = rows[0]["verdict"] if rows else None

    return {
        "dispatch_count": dispatch_count,
        "retry_count": retry_count,
        "last_verdict": last_verdict,
        "over_retry_limit": retry_count >= 2,
    }


def _since_clause_param(hours: float) -> str:
    return f"-{hours} hours"


def session_report(conn: sqlite3.Connection, since_hours: int = 24) -> str:
    """Build a markdown session report of actions + dispatches in the last N hours."""
    if since_hours <= 0:
        raise ValueError("'since_hours' must be a positive number.")

    since_param = _since_clause_param(since_hours)

    actions = conn.execute(
        "SELECT tier, category, description, project FROM actions "
        "WHERE ts >= datetime('now', ?) ORDER BY category, id",
        (since_param,),
    ).fetchall()

    dispatches = conn.execute(
        "SELECT task, agent_type, model, verdict FROM dispatches "
        "WHERE ts >= datetime('now', ?) ORDER BY id",
        (since_param,),
    ).fetchall()

    if not actions and not dispatches:
        return f"No entries in the last {since_hours}h."

    lines: list[str] = [f"## 自律実行リスト (last {since_hours}h)", ""]

    if actions:
        by_category: dict[str, list[sqlite3.Row]] = {}
        for row in actions:
            by_category.setdefault(row["category"], []).append(row)

        for category in sorted(by_category.keys()):
            lines.append(f"### {category}")
            for row in by_category[category]:
                suffix = f" ({row['project']})" if row["project"] else ""
                lines.append(f"- [Tier {row['tier']}] {row['description']}{suffix}")
            lines.append("")
    else:
        lines.append("(no actions)")
        lines.append("")

    lines.append("## Dispatches")
    if dispatches:
        for row in dispatches:
            verdict = row["verdict"] if row["verdict"] else "PENDING"
            lines.append(f"- {row['task']} → {row['agent_type']}/{row['model']} → {verdict}")
    else:
        lines.append("(no dispatches)")

    return "\n".join(lines)


def stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Aggregate stats over the last N days.

    {
      "actions_by_tier": {"0": int, "1": int, "2": int},
      "actions_by_category": {category: int, ...},
      "dispatch_count": int,
      "verdicts": {"CONFIRMED": int, "REFUTED": int, "PARTIAL": int, "PENDING": int}
    }
    """
    if days <= 0:
        raise ValueError("'days' must be a positive number.")

    since_param = f"-{days} days"

    actions_by_tier = {str(t): 0 for t in VALID_TIERS}
    for row in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM actions WHERE ts >= datetime('now', ?) GROUP BY tier",
        (since_param,),
    ).fetchall():
        actions_by_tier[str(row["tier"])] = row["n"]

    actions_by_category: dict[str, int] = {}
    for row in conn.execute(
        "SELECT category, COUNT(*) AS n FROM actions WHERE ts >= datetime('now', ?) "
        "GROUP BY category",
        (since_param,),
    ).fetchall():
        actions_by_category[row["category"]] = row["n"]

    dispatch_count = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE ts >= datetime('now', ?)",
        (since_param,),
    ).fetchone()["n"]

    verdicts = {v: 0 for v in VALID_VERDICTS}
    verdicts["PENDING"] = 0
    for row in conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM dispatches WHERE ts >= datetime('now', ?) "
        "GROUP BY verdict",
        (since_param,),
    ).fetchall():
        key = row["verdict"] if row["verdict"] else "PENDING"
        verdicts[key] = row["n"]

    return {
        "actions_by_tier": actions_by_tier,
        "actions_by_category": actions_by_category,
        "dispatch_count": int(dispatch_count),
        "verdicts": verdicts,
    }


def log_approach(
    conn: sqlite3.Connection,
    task: str,
    approach: str,
    outcome: str,
    reason: str,
) -> int:
    """Record the outcome of an approach tried (or considered) for a task.
    Returns the new row id.

    Raises ValueError with an actionable message if task/approach/reason are
    empty, or if outcome is not one of DEAD_END/NO_GO/WORKS.
    """
    task = _require_nonempty(task, "task")
    approach = _require_nonempty(approach, "approach")
    reason = _require_nonempty(reason, "reason")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"Invalid outcome {outcome!r}: must be one of {VALID_OUTCOMES} "
            "(DEAD_END=tried and failed, NO_GO=decided against without trying, "
            "WORKS=confirmed working)."
        )

    cur = conn.execute(
        "INSERT INTO approaches (task, approach, outcome, reason) VALUES (?, ?, ?, ?)",
        (task, approach, outcome, reason),
    )
    conn.commit()
    return int(cur.lastrowid)


def check_approaches(conn: sqlite3.Connection, task: str) -> dict:
    """Look up all recorded approaches for a task, bucketed by outcome.

    {
      "task": task,
      "dead_ends": [{"approach", "reason", "ts"}, ...],  # newest first
      "no_gos": [{"approach", "reason", "ts"}, ...],      # newest first
      "works": [{"approach", "reason", "ts"}, ...],       # newest first
      "total": int
    }
    """
    task = _require_nonempty(task, "task")

    rows = conn.execute(
        "SELECT approach, outcome, reason, ts FROM approaches WHERE task = ? ORDER BY id DESC",
        (task,),
    ).fetchall()

    dead_ends = []
    no_gos = []
    works = []
    for row in rows:
        item = {"approach": row["approach"], "reason": row["reason"], "ts": row["ts"]}
        if row["outcome"] == "DEAD_END":
            dead_ends.append(item)
        elif row["outcome"] == "NO_GO":
            no_gos.append(item)
        elif row["outcome"] == "WORKS":
            works.append(item)

    return {
        "task": task,
        "dead_ends": dead_ends,
        "no_gos": no_gos,
        "works": works,
        "total": len(rows),
    }
