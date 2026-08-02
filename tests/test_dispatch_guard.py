"""Tests for dispatch_guard.py — the Claude Code PreToolUse hook that
escalates to a human decision when a subagent dispatch is being
over-retried or a known dead end / no-go applies for the task.

Two layers of tests:
  1. In-process, calling dispatch_guard.main() with monkeypatched
     stdin/env against a tmp_path ledger DB — fast, exercises the logic.
  2. Real subprocess end-to-end tests (system python) to prove the script
     behaves correctly as an actual hook command: exit code 0 always,
     stdout is either empty or a single parseable JSON decision line.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import dispatch_guard
import ledger

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger_test.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_path))
    connection = ledger.get_connection()
    yield connection
    connection.close()


def run_guard(monkeypatch, stdin_text: str, capsys):
    """Invoke dispatch_guard.main() in-process with the given stdin.

    Returns (exit_code, stderr_text) — the guard signals a block with
    exit code 2 and the reason on stderr, so stderr is what callers assert
    against. exit 0 with empty stderr means "allow, silently".

    Also enforces the invariant that stdout is ALWAYS empty: the block
    signal is the exit code, never a stdout payload (a `permissionDecision`
    JSON on stdout is silently swallowed under permission mode `auto`).
    """
    monkeypatch.setattr(sys, "argv", ["dispatch_guard.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    exit_code = dispatch_guard.main()
    captured = capsys.readouterr()
    assert captured.out == "", f"guard must never write to stdout, got {captured.out!r}"
    return exit_code, captured.err


def _agent_event(task_description: str) -> dict:
    return {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "nagi-implementer",
            "model": "sonnet",
            "description": task_description,
            "prompt": "do the thing",
        },
    }


def _table_counts(connection) -> dict:
    counts = {}
    for table in ("actions", "dispatches", "approaches"):
        counts[table] = connection.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
    return counts


# --- clean task: silent ----------------------------------------------------


def test_clean_task_no_history_silent(conn, monkeypatch, capsys):
    event = _agent_event("brand new task never seen before")
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


# --- retry budget ------------------------------------------------------


def test_two_prior_dispatches_still_silent(conn, monkeypatch, capsys):
    task = "repeatedly dispatched task"
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 1")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 2")

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


def test_three_prior_dispatches_escalates(conn, monkeypatch, capsys):
    task = "over retried task"
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 1")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 2")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 3")

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 2, "over-retried task must BLOCK (exit 2), not merely warn"
    assert "Budget rule" in reason
    assert "3" in reason


# --- dead ends / no-gos --------------------------------------------------


def test_dead_end_escalates_even_with_no_retries(conn, monkeypatch, capsys):
    task = "task with a known dead end"
    ledger.log_approach(
        conn, task, "tried rewriting in rust", "DEAD_END", "toolchain unavailable in CI"
    )

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 2
    assert "tried rewriting in rust" in reason
    assert "toolchain unavailable in CI" in reason
    assert "DEAD_END" in reason


def test_no_go_escalates_even_with_no_retries(conn, monkeypatch, capsys):
    task = "task with a known no-go"
    ledger.log_approach(
        conn, task, "call the deprecated endpoint", "NO_GO", "explicitly forbidden by security"
    )

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 2
    assert "call the deprecated endpoint" in reason
    assert "explicitly forbidden by security" in reason
    assert "NO_GO" in reason


def test_works_only_does_not_escalate(conn, monkeypatch, capsys):
    task = "task with only a working approach"
    ledger.log_approach(conn, task, "the approach that worked", "WORKS", "confirmed passing")

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


# --- emitted JSON shape ----------------------------------------------------


def test_block_signal_shape(conn, monkeypatch, capsys):
    """The block signal is exit code 2 + a human-readable stderr reason.

    Guards against a regression back to the `permissionDecision: "ask"`
    JSON-on-stdout form, which permission mode `auto` silently swallows.
    """
    task = "shape check task"
    ledger.log_approach(conn, task, "some approach", "DEAD_END", "some reason")

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)

    assert exit_code == 2
    assert reason.startswith("BLOCKED by dispatch_guard.")
    assert "Task: shape check task" in reason
    assert "some approach" in reason
    # the reason must NOT be a JSON permission payload
    assert "permissionDecision" not in reason
    with pytest.raises(json.JSONDecodeError):
        json.loads(reason)


# --- robustness / silent no-ops --------------------------------------------


def test_non_agent_tool_name_silent(conn, monkeypatch, capsys):
    event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


def test_missing_tool_input_silent(conn, monkeypatch, capsys):
    event = {"tool_name": "Agent"}
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


def test_empty_stdin_silent(conn, monkeypatch, capsys):
    exit_code, out = run_guard(monkeypatch, "", capsys)
    assert exit_code == 0
    assert out == ""


def test_garbage_stdin_silent(conn, monkeypatch, capsys):
    exit_code, out = run_guard(monkeypatch, "{not valid json at all", capsys)
    assert exit_code == 0
    assert out == ""


def test_json_array_stdin_silent(conn, monkeypatch, capsys):
    exit_code, out = run_guard(monkeypatch, json.dumps([1, 2, 3]), capsys)
    assert exit_code == 0
    assert out == ""


def test_missing_ledger_db_fails_open(monkeypatch, tmp_path, capsys):
    # Point at a DB path whose parent cannot be created (a file where a
    # directory is expected), forcing get_connection() to raise. The guard
    # must still exit 0 and print nothing.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory")
    bogus_db_path = blocker / "nested" / "ledger.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(bogus_db_path))

    event = _agent_event("some task")
    exit_code, err = run_guard(monkeypatch, json.dumps(event), capsys)
    # fail-OPEN: allow the dispatch (exit 0). A diagnostic on stderr is
    # expected and fine; what must never happen is exit 2 (blocking every
    # dispatch because the guard itself is broken).
    assert exit_code == 0
    assert "dispatch_guard error" in err


# --- no-write proof ----------------------------------------------------


def test_guard_never_writes_to_ledger(conn, monkeypatch, capsys):
    task = "no write proof task"
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 1")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 2")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 3")
    ledger.log_approach(conn, task, "some dead end", "DEAD_END", "some reason")

    before = _table_counts(conn)

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 2  # this is the escalating case
    assert reason != ""

    after = _table_counts(conn)
    assert before == after


# --- truncation --------------------------------------------------------


def test_truncation_long_reason_and_many_dead_ends(conn, monkeypatch, capsys):
    task = "truncation stress task"
    long_reason = "x" * 5000
    for i in range(12):
        ledger.log_approach(conn, task, f"approach-{i}", "DEAD_END", long_reason)

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 2
    assert len(reason) <= 1400
    assert "+" in reason
    assert "more" in reason


def test_truncation_long_task_description(conn, monkeypatch, capsys):
    """Regression: only the long *approach reason* path was covered before
    (test_truncation_long_reason_and_many_dead_ends above). The *task
    description* is a separate untruncated input — derive_task() passes an
    Agent tool_input.description straight through with no length limit of
    its own, so a very long description (here ~5000 chars) must still be
    capped by build_reason's `_truncate(task, 100)` call, keeping the
    overall emitted reason within MAX_TOTAL_LEN — not just the task line
    truncated but the whole reason blown out to thousands of chars."""
    task = "T" * 5000
    ledger.log_approach(conn, task, "some approach", "DEAD_END", "some reason")

    event = _agent_event(task)
    exit_code, reason = run_guard(monkeypatch, json.dumps(event), capsys)

    assert exit_code == 2
    # documented cap: MAX_TOTAL_LEN plus the "BLOCKED by dispatch_guard.\n" prefix
    assert len(reason) <= dispatch_guard.MAX_TOTAL_LEN + len("BLOCKED by dispatch_guard.\n") + 10
    assert task not in reason  # the raw 5000-char description must never appear verbatim
    expected_task_line = f"Task: {dispatch_guard._truncate(task, 100)}"
    assert expected_task_line in reason
    assert "…" in reason


# --- end-to-end subprocess (system python) ---------------------------------


def _run_subprocess_guard(python_exe: str, event: dict, db_path: Path):
    import os

    env = {**os.environ, "NAGI_LEDGER_DB": str(db_path)}
    result = subprocess.run(
        [python_exe, str(REPO_ROOT / "dispatch_guard.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        encoding="utf-8",  # the guard writes UTF-8 bytes; do not use the OS codepage
        errors="replace",
        cwd=str(REPO_ROOT),
        env=env,
    )
    return result


def test_end_to_end_subprocess_escalating_case(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_escalating.db"
    task = "e2e escalating task"

    setup_conn = ledger.get_connection(db_path)
    try:
        ledger.log_approach(setup_conn, task, "e2e dead end approach", "DEAD_END", "e2e reason")
    finally:
        setup_conn.close()

    event = _agent_event(task)
    result = _run_subprocess_guard(system_python, event, db_path)

    assert result.returncode == 2, f"expected a BLOCK, got rc={result.returncode}"
    assert result.stdout == "", "block is signalled by exit code, never stdout"
    assert "BLOCKED by dispatch_guard." in result.stderr
    assert "e2e dead end approach" in result.stderr


def test_end_to_end_subprocess_non_ascii_reason_survives(tmp_path):
    """A Japanese reason must cross the subprocess boundary intact.

    Regression: the guard used to emit json.dumps(), which escaped
    non-ASCII to \\uXXXX and hid the problem. Writing plain text through
    sys.stderr on Windows uses the console codepage (cp932), which cannot
    encode these characters — hence the explicit UTF-8 byte write.
    """
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_utf8.db"
    task = "日本語タスク"
    approach = "SessionStart で stdin を読む"
    why = "セッションが起動不能になり、復旧は設定ファイルの手編集のみ"

    setup_conn = ledger.get_connection(db_path)
    try:
        ledger.log_approach(setup_conn, task, approach, "DEAD_END", why)
    finally:
        setup_conn.close()

    result = _run_subprocess_guard(system_python, _agent_event(task), db_path)

    assert result.returncode == 2
    assert approach in result.stderr
    assert why in result.stderr
    assert "\\u" not in result.stderr  # not escaped — real characters


def test_end_to_end_subprocess_clean_case(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_clean.db"
    event = _agent_event("e2e clean task never seen before")
    result = _run_subprocess_guard(system_python, event, db_path)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == ""
