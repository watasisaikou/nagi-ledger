"""Tests for session_brief.py — a SessionStart hook that prints a compact
markdown briefing of "open loops" left unfinished by a prior session.

Every test points NAGI_LEDGER_DB, NAGI_GOAL_FILE, and NAGI_BRIEF_REPOS at
tmp paths (the `brief_env` fixture) — this repo's own ledger/goal state and
this machine's real git repos (which may well be dirty) must never leak
into a test.

Layers:
  1. In-process, calling session_brief.main()/build_brief() against
     tmp_path-backed env — fast, exercises the gathering/formatting logic.
  2. Real subprocess end-to-end tests (system python) to prove the script
     behaves correctly as an actual SessionStart hook command.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import ledger
import session_brief

REPO_ROOT = Path(__file__).resolve().parent.parent


def _old_ts(days_ago: int) -> str:
    """Format a timestamp matching ledger's default ts shape
    (datetime('now') in sqlite -> 'YYYY-MM-DD HH:MM:SS', UTC)."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture()
def brief_env(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger.db"
    goal_file = tmp_path / "goal.json"
    # A NAGI_BRIEF_REPOS path that does not exist by default, so the dirty
    # repo section is deterministically empty unless a test overrides it.
    no_repos = tmp_path / "no_repos_here"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_path))
    monkeypatch.setenv("NAGI_GOAL_FILE", str(goal_file))
    monkeypatch.setenv("NAGI_BRIEF_REPOS", str(no_repos))
    return {"db_path": db_path, "goal_file": goal_file, "tmp_path": tmp_path}


def _run_main(monkeypatch, capsys, argv=None, stdin_text: str = "") -> tuple[int, str]:
    capsys.readouterr()
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin_text))
    exit_code = session_brief.main(argv or [])
    out = capsys.readouterr().out
    return exit_code, out


# --- everything empty ---------------------------------------------------


def test_everything_empty_prints_nothing(brief_env, monkeypatch, capsys):
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


# --- active goal ----------------------------------------------------------


def test_active_goal_appears(brief_env, monkeypatch, capsys):
    state = {
        "goal": "ship the widget",
        "remaining": 4,
        "max_turns": 10,
        "created": "2026-08-01T00:00:00+00:00",
    }
    brief_env["goal_file"].write_text(json.dumps(state), encoding="utf-8")

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out.startswith("## 開いてるループ")
    assert "### アクティブ goal" in out
    assert "ship the widget" in out
    assert "4" in out and "10" in out


# --- open verifications (dispatches) ---------------------------------------


def test_unverified_dispatch_appears_verified_does_not(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        _pending_id, _ = ledger.log_dispatch(
            conn, "task alpha pending", "nagi-implementer", "sonnet", "did stuff"
        )
        verified_id, _ = ledger.log_dispatch(
            conn, "task beta confirmed", "nagi-implementer", "sonnet", "did stuff"
        )
        ledger.log_verdict(conn, verified_id, "CONFIRMED", "checked out")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### 検証待ち" in out
    assert "task alpha pending" in out
    assert "nagi-implementer/sonnet" in out
    assert "task beta confirmed" not in out


def test_days_window_excludes_old_dispatches(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        conn.execute(
            "INSERT INTO dispatches (ts, task, agent_type, model, brief_summary, verdict) "
            "VALUES (?, ?, ?, ?, ?, NULL)",
            (_old_ts(10), "ancient unverified task", "nagi-implementer", "sonnet", "old"),
        )
        conn.commit()
        ledger.log_dispatch(conn, "recent unverified task", "nagi-implementer", "sonnet", "new")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys, argv=["--days", "3"])
    assert exit_code == 0
    assert "recent unverified task" in out
    assert "ancient unverified task" not in out


# --- recent dead ends -------------------------------------------------------


def test_deadend_and_nogo_appear_works_does_not(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        ledger.log_approach(conn, "task-a", "tried approach one", "DEAD_END", "crashed")
        ledger.log_approach(conn, "task-b", "tried approach two", "NO_GO", "too risky")
        ledger.log_approach(conn, "task-c", "tried approach three", "WORKS", "confirmed")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### 直近の dead-end" in out
    assert "task-a" in out and "tried approach one" in out
    assert "task-b" in out and "tried approach two" in out
    assert "task-c" not in out
    assert "tried approach three" not in out


# --- dirty repos --------------------------------------------------------------


def _git(*args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)


def _require_git():
    if not shutil.which("git"):
        pytest.skip("git not available")


def test_dirty_repo_appears_with_count(brief_env, monkeypatch, capsys):
    _require_git()
    repo_dir = brief_env["tmp_path"] / "dirty_repo"
    repo_dir.mkdir()
    assert _git("init", cwd=repo_dir).returncode == 0
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)
    (repo_dir / "untracked.txt").write_text("hello", encoding="utf-8")

    monkeypatch.setenv("NAGI_BRIEF_REPOS", str(repo_dir))
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### 未 commit" in out
    assert str(repo_dir) in out
    assert "1" in out


def test_clean_repo_absent(brief_env, monkeypatch, capsys):
    _require_git()
    repo_dir = brief_env["tmp_path"] / "clean_repo"
    repo_dir.mkdir()
    assert _git("init", cwd=repo_dir).returncode == 0

    monkeypatch.setenv("NAGI_BRIEF_REPOS", str(repo_dir))
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


def test_nonexistent_repo_path_skipped_no_crash(brief_env, monkeypatch, capsys):
    missing = brief_env["tmp_path"] / "does_not_exist_at_all"
    monkeypatch.setenv("NAGI_BRIEF_REPOS", str(missing))
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


def test_mixed_repo_list_dirty_and_missing(brief_env, monkeypatch, capsys):
    _require_git()
    repo_dir = brief_env["tmp_path"] / "mixed_dirty_repo"
    repo_dir.mkdir()
    assert _git("init", cwd=repo_dir).returncode == 0
    _git("config", "user.email", "test@example.com", cwd=repo_dir)
    _git("config", "user.name", "Test", cwd=repo_dir)
    (repo_dir / "untracked.txt").write_text("hello", encoding="utf-8")
    missing = brief_env["tmp_path"] / "still_missing"

    import os as _os

    monkeypatch.setenv("NAGI_BRIEF_REPOS", str(missing) + _os.pathsep + str(repo_dir))
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### 未 commit" in out
    assert str(repo_dir) in out


# --- _get_repo_list() default (no NAGI_BRIEF_REPOS override) ----------------


def test_get_repo_list_env_override_wins(monkeypatch):
    import os as _os

    monkeypatch.setenv("NAGI_BRIEF_REPOS", "/a/b" + _os.pathsep + "/c/d")
    assert session_brief._get_repo_list() == ["/a/b", "/c/d"]


def test_get_repo_list_default_outside_git_repo_is_empty(monkeypatch, tmp_path):
    """Regression: the old default fell back to hardcoded personal paths
    (~/.agents/skills, C:/Dev/nagi-ledger-mcp). With no override and cwd
    outside any git repo, the list must now be empty — never those paths."""
    monkeypatch.delenv("NAGI_BRIEF_REPOS", raising=False)
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)
    assert session_brief._get_repo_list() == []


def test_get_repo_list_default_inside_git_repo_returns_toplevel(monkeypatch, tmp_path):
    """With no override and cwd inside a git repo, the default is that
    repo's toplevel — derived at runtime, not any hardcoded personal path."""
    _require_git()
    repo_dir = tmp_path / "some_repo"
    repo_dir.mkdir()
    assert _git("init", cwd=repo_dir).returncode == 0
    monkeypatch.delenv("NAGI_BRIEF_REPOS", raising=False)
    monkeypatch.chdir(repo_dir)

    result = session_brief._get_repo_list()
    assert len(result) == 1
    assert Path(result[0]).resolve() == repo_dir.resolve()


# --- corrupt goal file -------------------------------------------------------


def test_corrupt_goal_file_no_crash_other_sections_render(brief_env, monkeypatch, capsys):
    brief_env["goal_file"].write_text("{not valid json at all", encoding="utf-8")
    conn = ledger.get_connection()
    try:
        ledger.log_dispatch(conn, "still shows up", "nagi-implementer", "sonnet", "x")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### アクティブ goal" not in out
    assert "### 検証待ち" in out
    assert "still shows up" in out


def test_goal_file_missing_required_keys_absent(brief_env, monkeypatch, capsys):
    brief_env["goal_file"].write_text(json.dumps({"goal": "incomplete"}), encoding="utf-8")
    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


# --- unopenable ledger DB (fail-open) -----------------------------------------


def test_ledger_db_unopenable_parent_is_file_goal_section_still_renders(
    brief_env, monkeypatch, capsys
):
    """Fail-open regression: if the ledger DB cannot be opened because its
    parent path is blocked by an existing file (so mkdir raises inside
    ledger.get_connection), session_brief must still exit 0 without
    crashing AND must not silently lose the sections that don't depend on
    the DB — only the DB-backed sections (dispatch / dead-end) should go
    missing. A fail-open that also loses working functionality is only
    half correct."""
    blocker = brief_env["tmp_path"] / "db_blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    bogus_db_path = blocker / "nested" / "ledger.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(bogus_db_path))

    state = {
        "goal": "goal survives a broken ledger db",
        "remaining": 3,
        "max_turns": 5,
        "created": "2026-08-01T00:00:00+00:00",
    }
    brief_env["goal_file"].write_text(json.dumps(state), encoding="utf-8")

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### アクティブ goal" in out
    assert "goal survives a broken ledger db" in out
    assert "### 検証待ち" not in out
    assert "### 直近の dead-end" not in out


def test_ledger_db_unopenable_path_is_directory_goal_section_still_renders(
    brief_env, monkeypatch, capsys
):
    """Same fail-open contract as above, via a different unopenable-DB
    shape: NAGI_LEDGER_DB points at a path that already exists as a
    directory (mkdir succeeds — the parent is fine — but sqlite3.connect()
    itself raises OperationalError). Regression guard: both distinct
    failure shapes must fail open without dropping non-DB sections."""
    db_as_dir = brief_env["tmp_path"] / "ledger_is_a_dir.db"
    db_as_dir.mkdir()
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_as_dir))

    state = {
        "goal": "goal survives db-path-is-a-directory",
        "remaining": 2,
        "max_turns": 4,
        "created": "2026-08-01T00:00:00+00:00",
    }
    brief_env["goal_file"].write_text(json.dumps(state), encoding="utf-8")

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert "### アクティブ goal" in out
    assert "goal survives db-path-is-a-directory" in out
    assert "### 検証待ち" not in out
    assert "### 直近の dead-end" not in out


# --- max-items / truncation --------------------------------------------------


def test_max_items_caps_list_length(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        for i in range(8):
            ledger.log_dispatch(conn, f"task number {i}", "nagi-implementer", "sonnet", "x")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys, argv=["--max-items", "2"])
    assert exit_code == 0
    count = out.count("task number")
    assert count == 2


def test_long_fields_truncated(brief_env, monkeypatch, capsys):
    long_task = "x" * 500
    conn = ledger.get_connection()
    try:
        ledger.log_dispatch(conn, long_task, "nagi-implementer", "sonnet", "x")
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert long_task not in out
    assert "x" * 71 not in out  # truncated well below the raw 500-char field


def test_total_output_capped_with_truncated_marker(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        for i in range(20):
            ledger.log_dispatch(
                conn, f"dispatch task {i} " + "y" * 60, "nagi-implementer", "sonnet", "x"
            )
            ledger.log_approach(
                conn,
                f"approach task {i} " + "z" * 60,
                "attempted approach " + "w" * 60,
                "DEAD_END",
                "reason",
            )
    finally:
        conn.close()

    exit_code, out = _run_main(monkeypatch, capsys, argv=["--max-items", "20"])
    assert exit_code == 0
    assert len(out) <= 1600  # ~1500 cap plus small slack for the marker/newlines
    if len(out) >= 1500:
        assert "(truncated)" in out


# --- no-write proof -----------------------------------------------------------


def test_no_write_proof(brief_env, monkeypatch, capsys):
    conn = ledger.get_connection()
    try:
        ledger.log_dispatch(conn, "some task", "nagi-implementer", "sonnet", "x")
        ledger.log_approach(conn, "some task", "some approach", "DEAD_END", "reason")
        ledger.log_action(conn, 0, "info", "an action")
    finally:
        conn.close()

    state = {
        "goal": "do not touch me",
        "remaining": 2,
        "max_turns": 5,
        "created": "2026-08-01T00:00:00+00:00",
    }
    brief_env["goal_file"].write_text(json.dumps(state), encoding="utf-8")
    goal_bytes_before = brief_env["goal_file"].read_bytes()

    def _row_counts():
        c = ledger.get_connection()
        try:
            return {
                "actions": c.execute("SELECT COUNT(*) AS n FROM actions").fetchone()["n"],
                "dispatches": c.execute("SELECT COUNT(*) AS n FROM dispatches").fetchone()["n"],
                "approaches": c.execute("SELECT COUNT(*) AS n FROM approaches").fetchone()["n"],
            }
        finally:
            c.close()

    before = _row_counts()

    exit_code, out = _run_main(monkeypatch, capsys)
    assert exit_code == 0
    assert out != ""  # sanity: this run actually gathered something

    after = _row_counts()
    goal_bytes_after = brief_env["goal_file"].read_bytes()

    assert before == after
    assert goal_bytes_before == goal_bytes_after


# --- end-to-end subprocess (system python) ----------------------------------


def _run_subprocess(
    python_exe: str, args: list[str], env: dict, stdin_text: str = ""
) -> subprocess.CompletedProcess:
    # Output contains Japanese headings; force UTF-8 on both ends so this
    # doesn't depend on the Windows console's active code page (cp932).
    env = {**env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
    return subprocess.run(
        [python_exe, str(REPO_ROOT / "session_brief.py"), *args],
        input=stdin_text,
        capture_output=True,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_end_to_end_subprocess_populated(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_ledger.db"
    goal_file = tmp_path / "e2e_goal.json"
    no_repos = tmp_path / "e2e_no_repos"
    env = {
        **__import__("os").environ,
        "NAGI_LEDGER_DB": str(db_path),
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_BRIEF_REPOS": str(no_repos),
    }

    state = {
        "goal": "e2e goal",
        "remaining": 3,
        "max_turns": 5,
        "created": "2026-08-01T00:00:00+00:00",
    }
    goal_file.write_text(json.dumps(state), encoding="utf-8")

    result = _run_subprocess(system_python, [], env, stdin_text=json.dumps({"session_id": "s1"}))
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "## 開いてるループ" in result.stdout
    assert "e2e goal" in result.stdout


def test_end_to_end_subprocess_empty(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "e2e_ledger2.db"
    goal_file = tmp_path / "e2e_goal2.json"
    no_repos = tmp_path / "e2e_no_repos2"
    env = {
        **__import__("os").environ,
        "NAGI_LEDGER_DB": str(db_path),
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_BRIEF_REPOS": str(no_repos),
    }

    result = _run_subprocess(system_python, [], env, stdin_text="")
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert result.stdout == ""


# --- regression: must never block on an open, unclosed stdin pipe ----------


def test_does_not_hang_on_open_unclosed_stdin_pipe(tmp_path):
    """Regression test for a real hang: session_brief.py used to call
    sys.stdin.read(), which blocks until EOF. A SessionStart hook must
    never wait on stdin — if the harness ever attaches a pipe it doesn't
    promptly close, that would hang session start forever with no recovery
    short of hand-editing settings.json. Spawn with stdin=PIPE and deliberately
    never write to or close it; the process must still exit promptly."""
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    db_path = tmp_path / "hang_ledger.db"
    goal_file = tmp_path / "hang_goal.json"
    no_repos = tmp_path / "hang_no_repos"
    env = {
        **__import__("os").environ,
        "NAGI_LEDGER_DB": str(db_path),
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_BRIEF_REPOS": str(no_repos),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }

    proc = subprocess.Popen(
        [system_python, str(REPO_ROOT / "session_brief.py")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        cwd=str(REPO_ROOT),
        env=env,
    )
    try:
        # Deliberately do NOT write to or close proc.stdin — the pipe stays
        # open, exactly like a harness that hasn't sent EOF yet.
        stdout, stderr = proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        pytest.fail(
            "session_brief.py hung for >5s with an open, unclosed stdin pipe "
            "— it must never read stdin."
        )

    assert proc.returncode == 0, f"stderr={stderr!r}"
    assert stdout == ""
