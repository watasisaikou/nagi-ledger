"""Tests for hook_ingest.py — the Claude Code hook entry point that
auto-records subagent dispatches and tool failures into the ledger.

Two layers of tests:
  1. In-process, calling hook_ingest.main() with monkeypatched stdin/argv/env
     against a tmp_path ledger DB — fast, exercises the parsing logic.
  2. Real subprocess end-to-end tests (both venv python and system python)
     to prove the script behaves correctly as an actual hook command:
     exit code 0 always, empty stdout, and DB side effects visible from a
     fresh process.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import hook_ingest
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


def run_hook(monkeypatch, event_type: str, stdin_text: str) -> int:
    """Invoke hook_ingest.main() in-process with the given argv/stdin."""
    monkeypatch.setattr(sys, "argv", ["hook_ingest.py", event_type])
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    return hook_ingest.main()


# --- agent-dispatch --------------------------------------------------------


def test_agent_dispatch_full_fields(conn, monkeypatch):
    event = {
        "session_id": "s1",
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "nagi-implementer",
            "model": "sonnet",
            "description": "fix the widget",
            "prompt": "Please fix the widget so it stops exploding.",
        },
        "tool_response": {},
    }
    exit_code = run_hook(monkeypatch, "agent-dispatch", json.dumps(event))
    assert exit_code == 0

    row = conn.execute("SELECT * FROM dispatches ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["task"] == "fix the widget"
    assert row["agent_type"] == "nagi-implementer"
    assert row["model"] == "sonnet"
    assert row["brief_summary"].startswith("[auto] ")
    assert "Please fix the widget" in row["brief_summary"]


def test_agent_dispatch_missing_description_falls_back_to_prompt(conn, monkeypatch):
    long_prompt = "word " * 40  # collapses to a single-spaced string > 80 chars
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "prompt": long_prompt,
        },
    }
    run_hook(monkeypatch, "agent-dispatch", json.dumps(event))

    row = conn.execute("SELECT * FROM dispatches ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    # ledger.log_dispatch strips all fields via _require_nonempty, so a
    # value that happens to end in whitespace after the 80-char slice
    # comes back stripped.
    expected_task_prefix = " ".join(long_prompt.split())[:80].strip()
    assert row["task"] == expected_task_prefix
    assert row["agent_type"] == "general-purpose"
    assert row["model"] == "inherit"


def test_agent_dispatch_missing_everything(conn, monkeypatch):
    event = {
        "tool_name": "Agent",
        "tool_input": {},
    }
    run_hook(monkeypatch, "agent-dispatch", json.dumps(event))

    row = conn.execute("SELECT * FROM dispatches ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["task"] == "unknown-dispatch"
    assert row["agent_type"] == "general-purpose"
    assert row["model"] == "inherit"
    assert row["brief_summary"] == "[auto] (no prompt)"


def test_agent_dispatch_wrong_tool_name_no_rows(conn, monkeypatch):
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
    }
    exit_code = run_hook(monkeypatch, "agent-dispatch", json.dumps(event))
    assert exit_code == 0

    count = conn.execute("SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    assert count == 0


def test_agent_dispatch_missing_tool_input_no_rows(conn, monkeypatch):
    event = {"tool_name": "Agent"}
    exit_code = run_hook(monkeypatch, "agent-dispatch", json.dumps(event))
    assert exit_code == 0

    count = conn.execute("SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    assert count == 0


# --- tool-failure ------------------------------------------------------


def test_tool_failure_creates_action_row(conn, monkeypatch):
    event = {
        "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /nonexistent"},
        "tool_response": {"error": "No such file or directory"},
    }
    exit_code = run_hook(monkeypatch, "tool-failure", json.dumps(event))
    assert exit_code == 0

    row = conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert row["tier"] == 0
    assert row["category"] == "tool_failure"
    assert row["description"] == "Bash: No such file or directory"
    assert row["project"] is None


def test_tool_failure_falls_back_to_stringified_response(conn, monkeypatch):
    event = {
        "tool_name": "WebFetch",
        "tool_input": {"url": "https://example.com"},
        "tool_response": "timeout after 30s",
    }
    run_hook(monkeypatch, "tool-failure", json.dumps(event))

    row = conn.execute("SELECT * FROM actions ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None
    assert "WebFetch" in row["description"]
    assert "timeout after 30s" in row["description"]


# --- robustness ----------------------------------------------------------


def test_invalid_json_no_rows_exit_zero(conn, monkeypatch):
    exit_code = run_hook(monkeypatch, "agent-dispatch", "{not valid json")
    assert exit_code == 0
    count = conn.execute("SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    assert count == 0


def test_empty_stdin_no_rows_exit_zero(conn, monkeypatch):
    exit_code = run_hook(monkeypatch, "agent-dispatch", "")
    assert exit_code == 0
    count = conn.execute("SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"]
    assert count == 0


# --- end-to-end subprocess -------------------------------------------------


def _run_subprocess_hook(python_exe: str, event_type: str, event: dict, db_path: Path):
    env = {**__import__("os").environ, "NAGI_LEDGER_DB": str(db_path)}
    result = subprocess.run(
        [python_exe, str(REPO_ROOT / "hook_ingest.py"), event_type],
        input=json.dumps(event),
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    return result


@pytest.mark.parametrize(
    "python_label,python_exe",
    [
        ("venv", str(VENV_PYTHON)),
        ("system", shutil.which("python") or shutil.which("python3") or ""),
    ],
)
def test_end_to_end_subprocess_agent_dispatch(tmp_path, python_label, python_exe):
    if not python_exe or not Path(python_exe).exists():
        pytest.skip(f"{python_label} python interpreter not found")

    db_path = tmp_path / f"e2e_{python_label}.db"
    event = {
        "tool_name": "Agent",
        "tool_input": {
            "subagent_type": "fork",
            "model": "opus",
            "description": "e2e subprocess test",
            "prompt": "verify hook_ingest works end to end as a real subprocess",
        },
    }
    result = _run_subprocess_hook(python_exe, "agent-dispatch", event, db_path)

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == ""

    conn = ledger.get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM dispatches ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        assert row["task"] == "e2e subprocess test"
        assert row["agent_type"] == "fork"
        assert row["model"] == "opus"
        assert row["brief_summary"].startswith("[auto] ")
    finally:
        conn.close()


def test_end_to_end_subprocess_invalid_json_empty_stdout_exit_zero(tmp_path):
    db_path = tmp_path / "e2e_invalid.db"
    env = {**__import__("os").environ, "NAGI_LEDGER_DB": str(db_path)}
    result = subprocess.run(
        [str(VENV_PYTHON), str(REPO_ROOT / "hook_ingest.py"), "agent-dispatch"],
        input="not json at all {{{",
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout == ""
    assert not db_path.exists()
