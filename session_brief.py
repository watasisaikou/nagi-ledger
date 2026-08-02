#!/usr/bin/env python3
"""
Claude Code SessionStart hook for nagi-ledger: prints a compact markdown
briefing of "open loops" — things left unfinished by a prior session — so a
fresh session starts already knowing what is outstanding.

Usage:
    python session_brief.py [--days N] [--max-items N]

Gathers, in priority order:
    1. アクティブ goal   — an active goal_gate.py goal (e.g. left set by a
                            crashed session).
    2. 検証待ち          — dispatches with no verdict yet, within --days.
    3. 直近の dead-end    — approaches recorded DEAD_END/NO_GO, within --days.
    4. 未 commit          — configured repos with a dirty `git status`. The
                            repo list comes from NAGI_BRIEF_REPOS (PATH-style,
                            separated by os.pathsep) if set; otherwise it is
                            the current working directory's git toplevel (via
                            `git rev-parse --show-toplevel`), or an empty list
                            if cwd is not inside a git repo.

Whatever this script prints on stdout is injected directly into the new
session's context (SessionStart hook contract), so output is deliberately
terse and capped (~1500 chars): every byte costs context on EVERY session
start. When there is nothing outstanding, it prints NOTHING at all.

Fail-open: any exception anywhere results in printing nothing and exiting 0
— this must never break or slow down session start. Diagnostics, if any, go
to stderr only.

This script never reads stdin, even though the SessionStart hook contract
pipes an event JSON to it. A blocking `sys.stdin.read()` would hang session
start forever if stdin is ever attached to something that isn't closed
promptly, with no recovery short of hand-editing settings.json — worse than
any other failure mode here. The event payload isn't needed, so the safe
choice is to never look at it.

Deliberately stdlib-only (json/os/subprocess/sys/time + ledger, which is
itself pure sqlite3/os/pathlib) so it can run under ANY python interpreter
that has sqlite3 available — the venv python or the system python — without
needing `mcp` installed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# hook cwd is not guaranteed to be this directory, so resolve the ledger
# import relative to this file rather than relying on cwd-based sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import ledger  # noqa: E402  (import after sys.path fixup, intentional)

DEFAULT_DAYS = 3
DEFAULT_MAX_ITEMS = 5

MAX_TOTAL_LEN = 1500
TRUNCATED_MARKER = "(truncated)"

GIT_TIMEOUT_SECONDS = 3
GIT_TOTAL_BUDGET_SECONDS = 4.0

HEADER = "## 開いてるループ"

GOAL_REQUIRED_KEYS = {"goal", "remaining", "max_turns", "created"}


def _truncate(text, limit: int) -> str:
    text = text if isinstance(text, str) else str(text)
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + "…"


# --- section 1: active goal --------------------------------------------------


def _goal_file_path() -> Path:
    override = os.environ.get("NAGI_GOAL_FILE")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".nagi" / "goal.json"


def _load_goal_state() -> dict | None:
    """Read the goal_gate.py state file directly. Never raises: any
    read/parse failure or missing-key shape is treated as "no active goal"."""
    path = _goal_file_path()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if not GOAL_REQUIRED_KEYS.issubset(data.keys()):
        return None
    return data


def _build_goal_section() -> list[str]:
    state = _load_goal_state()
    if state is None:
        return []
    goal_text = _truncate(state.get("goal", ""), 200)
    remaining = state.get("remaining")
    max_turns = state.get("max_turns")
    return [
        "### アクティブ goal",
        f"- {goal_text} (remaining {remaining}/{max_turns})",
    ]


# --- section 2: open verifications (unverified dispatches) ------------------


def _build_dispatch_section(conn, days: int, max_items: int) -> list[str]:
    rows = conn.execute(
        "SELECT task, agent_type, model FROM dispatches "
        "WHERE verdict IS NULL AND ts >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (f"-{days} days", max_items),
    ).fetchall()
    if not rows:
        return []
    lines = ["### 検証待ち"]
    for row in rows:
        task = _truncate(row["task"], 70)
        lines.append(f"- {task} — {row['agent_type']}/{row['model']}")
    return lines


# --- section 3: recent dead ends ---------------------------------------------


def _build_deadend_section(conn, days: int, max_items: int) -> list[str]:
    rows = conn.execute(
        "SELECT task, approach FROM approaches "
        "WHERE outcome IN ('DEAD_END', 'NO_GO') AND ts >= datetime('now', ?) "
        "ORDER BY id DESC LIMIT ?",
        (f"-{days} days", max_items),
    ).fetchall()
    if not rows:
        return []
    lines = ["### 直近の dead-end"]
    for row in rows:
        task = _truncate(row["task"], 60)
        approach = _truncate(row["approach"], 60)
        lines.append(f"- {task}: {approach}")
    return lines


# --- section 4: dirty repos ---------------------------------------------------


def _current_repo_toplevel() -> list[str]:
    """Return [toplevel] if cwd is inside a git repo, else [].

    Never raises: git missing, timing out, or cwd not being a repo are all
    treated the same way — no repos to check.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    toplevel = proc.stdout.strip()
    return [toplevel] if toplevel else []


def _get_repo_list() -> list[str]:
    override = os.environ.get("NAGI_BRIEF_REPOS")
    if override:
        return [p for p in override.split(os.pathsep) if p.strip()]
    return _current_repo_toplevel()


def _check_dirty_repos(repos: list[str]) -> list[tuple[str, int]]:
    """Run `git status --porcelain` per repo. Silently skips paths that
    don't exist, aren't repos, or where git is missing/times out. The total
    wall clock spent across all git subprocesses is hard-capped at
    GIT_TOTAL_BUDGET_SECONDS: each repo's subprocess timeout is the smaller
    of GIT_TIMEOUT_SECONDS and whatever budget remains, so a run of slow/
    hanging repos cannot exceed the overall budget."""
    results: list[tuple[str, int]] = []
    start = time.monotonic()
    for repo in repos:
        remaining_budget = GIT_TOTAL_BUDGET_SECONDS - (time.monotonic() - start)
        if remaining_budget <= 0:
            break
        path = Path(repo)
        try:
            if not path.exists() or not path.is_dir():
                continue
        except Exception:
            continue
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), "status", "--porcelain"],
                capture_output=True,
                text=True,
                timeout=min(GIT_TIMEOUT_SECONDS, remaining_budget),
            )
        except Exception:
            # git missing, timed out, or any other subprocess failure.
            continue
        if proc.returncode != 0:
            continue
        changed = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        if changed:
            results.append((str(path), len(changed)))
    return results


def _build_dirty_section() -> list[str]:
    dirty = _check_dirty_repos(_get_repo_list())
    if not dirty:
        return []
    lines = ["### 未 commit"]
    for path, count in dirty:
        lines.append(f"- {path} ({count} changed)")
    return lines


# --- assembly -----------------------------------------------------------------


def _open_ledger_connection():
    try:
        return ledger.get_connection()
    except Exception:
        return None


def _assemble(sections: list[list[str]], truncated: bool) -> str:
    parts = [HEADER]
    for section in sections:
        parts.append("")
        parts.extend(section)
    if truncated:
        parts.append("")
        parts.append(TRUNCATED_MARKER)
    return "\n".join(parts)


def build_brief(days: int, max_items: int) -> str:
    """Build the full markdown brief, or "" if there is nothing outstanding.

    Never writes to the ledger or the goal file. Section priority (for
    truncation, lowest priority dropped first) is: goal > 検証待ち >
    直近の dead-end > 未 commit.
    """
    all_sections: list[list[str]] = []

    goal_section = _build_goal_section()
    if goal_section:
        all_sections.append(goal_section)

    conn = _open_ledger_connection()
    if conn is not None:
        try:
            dispatch_section = _build_dispatch_section(conn, days, max_items)
            if dispatch_section:
                all_sections.append(dispatch_section)

            deadend_section = _build_deadend_section(conn, days, max_items)
            if deadend_section:
                all_sections.append(deadend_section)
        finally:
            conn.close()

    dirty_section = _build_dirty_section()
    if dirty_section:
        all_sections.append(dirty_section)

    if not all_sections:
        return ""

    truncated = False
    output = _assemble(all_sections, truncated)

    # Drop lowest-priority sections (from the end) until it fits.
    while len(output) > MAX_TOTAL_LEN and len(all_sections) > 1:
        all_sections.pop()
        truncated = True
        output = _assemble(all_sections, truncated)

    if len(output) > MAX_TOTAL_LEN:
        # Even the single highest-priority section doesn't fit: hard-cut.
        truncated = True
        marker = f"\n\n{TRUNCATED_MARKER}"
        output = output[: MAX_TOTAL_LEN - len(marker)] + marker

    return output


# --- CLI ------------------------------------------------------------------


def _parse_args(argv: list[str]) -> tuple[int, int]:
    days = DEFAULT_DAYS
    max_items = DEFAULT_MAX_ITEMS
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--days" and i + 1 < len(argv):
            try:
                days = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        if arg == "--max-items" and i + 1 < len(argv):
            try:
                max_items = int(argv[i + 1])
            except ValueError:
                pass
            i += 2
            continue
        i += 1
    if days <= 0:
        days = DEFAULT_DAYS
    if max_items <= 0:
        max_items = DEFAULT_MAX_ITEMS
    return days, max_items


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    # Deliberately never touch sys.stdin: a SessionStart hook must never
    # block session start, and reading stdin blocks until EOF — if stdin is
    # ever attached to an unclosed pipe, that hang has no recovery short of
    # hand-editing settings.json. The event payload is not needed by this
    # tool, so "tolerate any stdin" here means "never look at it," not
    # "drain it safely."
    days, max_items = _parse_args(argv)
    output = build_brief(days, max_items)
    if output:
        print(output)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # fail-open: never let the hook break session start
        try:
            print(f"session_brief error: {exc}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(0)
