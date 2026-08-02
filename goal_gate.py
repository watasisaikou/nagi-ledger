#!/usr/bin/env python3
"""
Deterministic "keep working until done" gate for Claude Code's Stop hook.

When a goal is active, the Stop hook (wired to `goal_gate.py stop-gate`)
blocks the agent from ending its turn until the agent explicitly declares
the goal done (via `goal_gate.py done`) or aborted (`goal_gate.py abort`),
or the turn budget runs out.

CLI:
    python goal_gate.py set "<goal text>" [--max-turns N]
    python goal_gate.py done [note]
    python goal_gate.py abort [reason]
    python goal_gate.py status
    python goal_gate.py stop-gate      # hook mode, reads event JSON on stdin

State file (single active goal): JSON at %USERPROFILE%\\.nagi\\goal.json,
overridable via env NAGI_GOAL_FILE.

History (append-only, one JSON object per line): %USERPROFILE%\\.nagi\\goal_history.jsonl,
overridable via env NAGI_GOAL_HISTORY.

Deliberately stdlib-only so it can run under ANY python interpreter
(venv or system) without needing anything installed — this is what makes
it safe to call from settings.json as a Stop hook command.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_MAX_TURNS = 10
MIN_MAX_TURNS = 1
MAX_MAX_TURNS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _goal_file() -> Path:
    override = os.environ.get("NAGI_GOAL_FILE")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".nagi" / "goal.json"


def _history_file() -> Path:
    override = os.environ.get("NAGI_GOAL_HISTORY")
    if override:
        return Path(override)
    return Path(os.path.expanduser("~")) / ".nagi" / "goal_history.jsonl"


def _load_state() -> dict | None:
    """Return the active goal state dict, or None if absent/unparseable.

    Never raises: any read/parse failure is treated as "no active goal".
    """
    path = _goal_file()
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    required = {"goal", "remaining", "max_turns", "created"}
    if not required.issubset(data.keys()):
        return None
    return data


def _save_state(state: dict) -> None:
    path = _goal_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state), encoding="utf-8")


def _delete_state() -> None:
    path = _goal_file()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _append_history(state: dict, outcome: str, note: str | None) -> None:
    path = _history_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    max_turns = state.get("max_turns", 0)
    remaining = state.get("remaining", 0)
    try:
        turns_used = int(max_turns) - int(remaining)
    except Exception:
        turns_used = None
    entry = {
        "goal": state.get("goal"),
        "outcome": outcome,
        "note": note,
        "created": state.get("created"),
        "closed": _now_iso(),
        "turns_used": turns_used,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


# --- commands ---------------------------------------------------------------


def cmd_set(args: list[str]) -> int:
    if not args:
        print("error: 'set' requires a goal text argument", file=sys.stderr)
        return 1

    max_turns = DEFAULT_MAX_TURNS
    goal_parts: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--max-turns":
            if i + 1 >= len(args):
                print("error: --max-turns requires a value", file=sys.stderr)
                return 1
            try:
                max_turns = int(args[i + 1])
            except ValueError:
                print(f"error: --max-turns must be an integer, got {args[i + 1]!r}", file=sys.stderr)
                return 1
            i += 2
            continue
        goal_parts.append(arg)
        i += 1

    goal_text = " ".join(goal_parts).strip()
    if not goal_text:
        print("error: goal text must not be empty", file=sys.stderr)
        return 1

    if not (MIN_MAX_TURNS <= max_turns <= MAX_MAX_TURNS):
        print(
            f"error: --max-turns must be between {MIN_MAX_TURNS} and {MAX_MAX_TURNS}, got {max_turns}",
            file=sys.stderr,
        )
        return 1

    existing = _load_state()
    if existing is not None:
        _append_history(existing, "superseded", None)

    state = {
        "goal": goal_text,
        "remaining": max_turns,
        "max_turns": max_turns,
        "created": _now_iso(),
    }
    _save_state(state)
    print(f"goal set: {goal_text} (max_turns={max_turns})")
    return 0


def cmd_extend(args: list[str]) -> int:
    """Add turns to the active goal's budget.

    Exists because the gate cannot distinguish "stopped early" from
    "legitimately blocked waiting on background agents" — waiting would
    otherwise burn the budget one turn per check.
    """
    if not args:
        print("error: 'extend' requires a number of turns", file=sys.stderr)
        return 1
    try:
        n = int(args[0])
    except ValueError:
        print(f"error: extend requires an integer, got {args[0]!r}", file=sys.stderr)
        return 1
    if not (MIN_MAX_TURNS <= n <= MAX_MAX_TURNS):
        print(
            f"error: extend amount must be between {MIN_MAX_TURNS} and {MAX_MAX_TURNS}, got {n}",
            file=sys.stderr,
        )
        return 1

    state = _load_state()
    if state is None:
        print("error: no active goal", file=sys.stderr)
        return 1
    try:
        remaining = int(state.get("remaining", 0))
        max_turns = int(state.get("max_turns", 0))
    except (TypeError, ValueError):
        print("error: corrupt goal state (remaining/max_turns not numeric)", file=sys.stderr)
        return 1

    state["remaining"] = remaining + n
    state["max_turns"] = max_turns + n
    state["extensions"] = int(state.get("extensions", 0)) + 1
    _save_state(state)
    print(
        f"goal extended by {n} (remaining={state['remaining']}/{state['max_turns']}, "
        f"extensions={state['extensions']})"
    )
    return 0


def _close_active(outcome: str, args: list[str]) -> int:
    note = " ".join(args).strip() or None
    state = _load_state()
    if state is None:
        print("error: no active goal", file=sys.stderr)
        return 1
    _append_history(state, outcome, note)
    _delete_state()
    print(f"goal {outcome}: {state.get('goal')}")
    return 0


def cmd_done(args: list[str]) -> int:
    return _close_active("done", args)


def cmd_abort(args: list[str]) -> int:
    return _close_active("aborted", args)


def cmd_status(_args: list[str]) -> int:
    state = _load_state()
    if state is None:
        print("no active goal")
        return 0
    print(
        f"active goal: {state.get('goal')} "
        f"(remaining={state.get('remaining')}/{state.get('max_turns')}, "
        f"created={state.get('created')})"
    )
    return 0


def _read_stdin_text() -> str:
    try:
        raw = sys.stdin.read()
    except Exception:
        return ""
    return raw or ""


def cmd_stop_gate(_args: list[str]) -> int:
    # Tolerate any input shape — we don't actually need the event contents,
    # just need to never crash while draining stdin.
    _read_stdin_text()

    path = _goal_file()
    if not path.exists():
        return 0

    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("state is not a dict")
        required = {"goal", "remaining", "max_turns", "created"}
        if not required.issubset(data.keys()):
            raise ValueError("state missing required keys")
    except Exception:
        # Corrupt/unparseable state file: clear it and record the fact.
        placeholder_state = {"goal": None, "max_turns": 0, "remaining": 0, "created": None}
        _append_history(placeholder_state, "corrupt_state", None)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return 0

    state = data
    try:
        remaining = int(state.get("remaining"))
    except Exception:
        remaining = 0
    remaining -= 1
    state["remaining"] = remaining
    _save_state(state)

    goal_text = str(state.get("goal", ""))
    max_turns = state.get("max_turns")

    if remaining <= 0:
        _append_history(state, "budget_exhausted", None)
        _delete_state()
        goal_excerpt = goal_text[:120]
        print(json.dumps({
            "systemMessage": f"[goal-gate] turn budget exhausted ({max_turns} turns); goal cleared: {goal_excerpt}"
        }))
        return 0

    reason = (
        f"[goal-gate] Active goal: {goal_text}. Turns left: {remaining}. "
        f"If the goal is COMPLETE and you have VERIFIED it with evidence, run: "
        f'python C:/Dev/nagi-ledger-mcp/goal_gate.py done "<one-line evidence>" and then stop. '
        f"If you cannot complete it, run: "
        f'python C:/Dev/nagi-ledger-mcp/goal_gate.py abort "<reason>". '
        f"Otherwise continue working toward the goal now."
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


COMMANDS = {
    "set": cmd_set,
    "extend": cmd_extend,
    "done": cmd_done,
    "abort": cmd_abort,
    "status": cmd_status,
    "stop-gate": cmd_stop_gate,
}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: goal_gate.py <set|done|abort|status|stop-gate> ...", file=sys.stderr)
        return 1
    command = argv[0]
    handler = COMMANDS.get(command)
    if handler is None:
        print(f"error: unknown command {command!r}", file=sys.stderr)
        return 1
    return handler(argv[1:])


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stop-gate":
        # stop-gate must NEVER exit non-zero and NEVER print anything except
        # the intended single-line JSON (or nothing, if no active goal).
        try:
            sys.exit(cmd_stop_gate(sys.argv[2:]))
        except Exception:
            sys.exit(0)
    else:
        sys.exit(main())
