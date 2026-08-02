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
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger_test.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_path))
    connection = ledger.get_connection()
    yield connection
    connection.close()


def run_guard(monkeypatch, stdin_text: str, capsys):
    """Invoke dispatch_guard.main() in-process with the given stdin.

    Returns (exit_code, stdout_text).
    """
    monkeypatch.setattr(sys, "argv", ["dispatch_guard.py"])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    exit_code = dispatch_guard.main()
    captured = capsys.readouterr()
    return exit_code, captured.out


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
        counts[table] = connection.execute(
            f"SELECT COUNT(*) AS n FROM {table}"
        ).fetchone()["n"]
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
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out != ""

    payload = json.loads(out)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Budget rule" in reason
    assert "3" in reason


# --- dead ends / no-gos --------------------------------------------------


def test_dead_end_escalates_even_with_no_retries(conn, monkeypatch, capsys):
    task = "task with a known dead end"
    ledger.log_approach(
        conn, task, "tried rewriting in rust", "DEAD_END", "toolchain unavailable in CI"
    )

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out != ""

    payload = json.loads(out)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert "tried rewriting in rust" in reason
    assert "toolchain unavailable in CI" in reason
    assert "DEAD_END" in reason


def test_no_go_escalates_even_with_no_retries(conn, monkeypatch, capsys):
    task = "task with a known no-go"
    ledger.log_approach(
        conn, task, "call the deprecated endpoint", "NO_GO", "explicitly forbidden by security"
    )

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out != ""

    payload = json.loads(out)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
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


def test_emitted_json_shape(conn, monkeypatch, capsys):
    task = "shape check task"
    ledger.log_approach(conn, task, "some approach", "DEAD_END", "some reason")

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0

    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == "ask"
    assert isinstance(hso["permissionDecisionReason"], str)
    assert len(hso.keys()) >= 3
    # exactly the expected keys, no stray extras
    assert set(hso.keys()) == {"hookEventName", "permissionDecision", "permissionDecisionReason"}


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
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out == ""


# --- no-write proof ----------------------------------------------------


def test_guard_never_writes_to_ledger(conn, monkeypatch, capsys):
    task = "no write proof task"
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 1")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 2")
    ledger.log_dispatch(conn, task, "nagi-implementer", "sonnet", "attempt 3")
    ledger.log_approach(conn, task, "some dead end", "DEAD_END", "some reason")

    before = _table_counts(conn)

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out != ""  # this is the escalating case

    after = _table_counts(conn)
    assert before == after


# --- truncation --------------------------------------------------------


def test_truncation_long_reason_and_many_dead_ends(conn, monkeypatch, capsys):
    task = "truncation stress task"
    long_reason = "x" * 5000
    for i in range(12):
        ledger.log_approach(conn, task, f"approach-{i}", "DEAD_END", long_reason)

    event = _agent_event(task)
    exit_code, out = run_guard(monkeypatch, json.dumps(event), capsys)
    assert exit_code == 0
    assert out != ""

    payload = json.loads(out)
    reason = payload["hookSpecificOutput"]["permissionDecisionReason"]
    assert len(reason) <= 1400
    assert "+" in reason
    assert "more" in reason


# --- end-to-end subprocess (system python) ---------------------------------


def _run_subprocess_guard(python_exe: str, event: dict, db_path: Path):
    import os

    env = {**os.environ, "NAGI_LEDGER_DB": str(db_path)}
    result = subprocess.run(
        [python_exe, str(REPO_ROOT / "dispatch_guard.py")],
        input=json.dumps(event),
        capture_output=True,
        text=True,
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

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout.strip() != ""
    payload = json.loads(result.stdout.strip())
    assert payload["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert payload["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert "e2e dead end approach" in payload["hookSpecificOutput"]["permissionDecisionReason"]


def test_end_to_end_subprocess_clean_case(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_clean.db"
    event = _agent_event("e2e clean task never seen before")
    result = _run_subprocess_guard(system_python, event, db_path)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == ""
