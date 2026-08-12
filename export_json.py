#!/usr/bin/env python3
"""
Read-only JSON export of the ledger, for external viewers.

Usage:
    python export_json.py                 # default DB (ledger.get_db_path()) to stdout
    python export_json.py --out FILE      # write to FILE (UTF-8, LF newlines)
    python export_json.py --db PATH       # use an explicit DB path

Emits a single JSON object:
    {
      "schema": "nagi-ledger-export/1",
      "exported_at": "YYYY-MM-DD HH:MM:SS",   # UTC, same format as the ledger's own ts columns
      "actions":    [...],   # full actions table, id ascending
      "dispatches": [...],   # full dispatches table, id ascending
      "approaches": [...]    # full approaches table, id ascending
    }

This is a companion to ledger-view (github.com/watasisaikou/ledger-view), a
plain TS SPA that reads exactly this shape. The key names, types, and array
order below are a CONTRACT shared with that project's parser — do not rename,
reorder, or drop a column here without updating it there too.

Every row is included, in full, for all three tables: no filtering, no
paging. Deciding what to show is the viewer's job, not this script's.

Opens the DB read-only (sqlite3 URI mode=ro) and never creates one: unlike
ledger.get_connection(), this never calls init_db(). If the target DB does
not exist, this prints an error to stderr and exits 1 rather than silently
producing an export of nothing.

This is a plain CLI tool run by hand or by ledger-view's build step, not a
Claude Code hook — there is no fail-open requirement here. Bad arguments and
a missing DB fail loudly (stderr + exit 1) on purpose.

Deliberately stdlib-only (json/sqlite3/sys + ledger, which is itself pure
sqlite3/os/pathlib) so it can run under ANY python interpreter that has
sqlite3 available — the venv python or the system python — without needing
`mcp` installed.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

# cwd is not guaranteed to be this directory, so resolve the ledger import
# relative to this file rather than relying on cwd-based sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger

SCHEMA_VERSION = "nagi-ledger-export/1"


def _now_str() -> str:
    """UTC timestamp in the same 'YYYY-MM-DD HH:MM:SS' shape as the ledger's
    own ts columns (sqlite's datetime('now'), which is also UTC)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open db_path strictly read-only via a sqlite3 URI.

    Never creates the file or its schema (unlike ledger.get_connection(),
    which calls init_db() on every open) — existence is checked by the
    caller first, and mode=ro additionally makes sqlite refuse to write
    even if this code later tried to.
    """
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _actions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, tier, category, description, project FROM actions ORDER BY id ASC"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "ts": row["ts"],
            "tier": row["tier"],
            "category": row["category"],
            "description": row["description"],
            "project": row["project"],
        }
        for row in rows
    ]


def _dispatches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, task, agent_type, model, brief_summary, verdict, verdict_ts, "
        "verdict_notes FROM dispatches ORDER BY id ASC"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "ts": row["ts"],
            "task": row["task"],
            "agent_type": row["agent_type"],
            "model": row["model"],
            "brief_summary": row["brief_summary"],
            "verdict": row["verdict"],
            "verdict_ts": row["verdict_ts"],
            "verdict_notes": row["verdict_notes"],
        }
        for row in rows
    ]


def _approaches(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id, ts, task, approach, outcome, reason FROM approaches ORDER BY id ASC"
    ).fetchall()
    return [
        {
            "id": row["id"],
            "ts": row["ts"],
            "task": row["task"],
            "approach": row["approach"],
            "outcome": row["outcome"],
            "reason": row["reason"],
        }
        for row in rows
    ]


def build_export(conn: sqlite3.Connection) -> dict:
    """Assemble the export dict. Key order matches the documented contract
    (dict insertion order is preserved by json.dumps)."""
    return {
        "schema": SCHEMA_VERSION,
        "exported_at": _now_str(),
        "actions": _actions(conn),
        "dispatches": _dispatches(conn),
        "approaches": _approaches(conn),
    }


# --- CLI ------------------------------------------------------------------


def _write_stdout(text: str) -> None:
    """Write text + a trailing newline to stdout as explicit UTF-8 bytes.

    Not print(text): recorded task/description/reason text is often
    Japanese, and on a non-UTF-8 Windows console (cp932 is common) plain
    print() encodes with sys.stdout.encoding and can raise or mangle it.
    dispatch_guard.py hit exactly this for stderr and fixed it the same way
    (see emit_block there) — this mirrors that fix for stdout.
    """
    buf = getattr(sys.stdout, "buffer", None)  # absent under pytest's capsys
    if buf is not None:
        buf.write((text + "\n").encode("utf-8", errors="replace"))
        buf.flush()
        return
    print(text)


def _parse_args(argv: list[str]) -> tuple[str | None, str | None]:
    """Parse --out/--db. Raises ValueError (with an actionable message) on
    an unknown flag or a flag missing its value — this tool is run by hand,
    not as a hook, so there is no reason to swallow a typo."""
    out_path: str | None = None
    db_path: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--out":
            if i + 1 >= len(argv):
                raise ValueError("--out requires a value")
            out_path = argv[i + 1]
            i += 2
            continue
        if arg == "--db":
            if i + 1 >= len(argv):
                raise ValueError("--db requires a value")
            db_path = argv[i + 1]
            i += 2
            continue
        raise ValueError(f"unknown argument: {arg!r}")
    return out_path, db_path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    try:
        out_arg, db_arg = _parse_args(argv)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    db_path = Path(db_arg) if db_arg else ledger.get_db_path()
    if not db_path.exists():
        print(f"error: ledger DB not found at {db_path}", file=sys.stderr)
        return 1

    conn = _open_readonly(db_path)
    try:
        export = build_export(conn)
    finally:
        conn.close()

    text = json.dumps(export, ensure_ascii=False, indent=2)

    if out_arg:
        # Explicit newline="\n": Path.write_text()'s default text-mode write
        # translates "\n" to os.linesep, which is "\r\n" on Windows. The
        # contract with ledger-view promises LF regardless of platform.
        Path(out_arg).write_text(text + "\n", encoding="utf-8", newline="\n")
    else:
        _write_stdout(text)

    return 0


if __name__ == "__main__":
    sys.exit(main())
