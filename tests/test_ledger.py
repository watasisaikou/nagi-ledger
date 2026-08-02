"""Tests for ledger.py — pure logic, no MCP involved.

Each test points NAGI_LEDGER_DB at a fresh tmp_path file so tests never
touch the real ledger DB.
"""

from __future__ import annotations

import sqlite3

import pytest

import ledger


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger_test.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_path))
    connection = ledger.get_connection()
    yield connection
    connection.close()


def test_get_db_path_uses_env_var(tmp_path, monkeypatch):
    target = tmp_path / "custom" / "ledger.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(target))
    assert ledger.get_db_path() == target


def test_get_db_path_default_when_unset(monkeypatch):
    monkeypatch.delenv("NAGI_LEDGER_DB", raising=False)
    path = ledger.get_db_path()
    assert path.name == "ledger.db"
    assert path.parent.name == ".nagi"


def test_schema_creates_both_tables(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "actions" in tables
    assert "dispatches" in tables


def test_get_connection_creates_parent_dir(tmp_path, monkeypatch):
    nested = tmp_path / "does" / "not" / "exist" / "ledger.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(nested))
    connection = ledger.get_connection()
    try:
        assert nested.parent.exists()
        assert nested.exists()
    finally:
        connection.close()


# --- log_action ---------------------------------------------------------

def test_log_action_success(conn):
    action_id = ledger.log_action(conn, 1, "refactor", "cleaned up server.py", "nagi-ledger")
    assert isinstance(action_id, int)
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert row["tier"] == 1
    assert row["category"] == "refactor"
    assert row["description"] == "cleaned up server.py"
    assert row["project"] == "nagi-ledger"


def test_log_action_project_optional(conn):
    action_id = ledger.log_action(conn, 0, "note", "just a note")
    row = conn.execute("SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    assert row["project"] is None


@pytest.mark.parametrize("bad_tier", [-1, 3, 99, None, "1"])
def test_log_action_rejects_bad_tier(conn, bad_tier):
    with pytest.raises(ValueError):
        ledger.log_action(conn, bad_tier, "cat", "desc")


def test_log_action_rejects_empty_category(conn):
    with pytest.raises(ValueError):
        ledger.log_action(conn, 1, "   ", "desc")


def test_log_action_rejects_empty_description(conn):
    with pytest.raises(ValueError):
        ledger.log_action(conn, 1, "cat", "")


# --- log_dispatch --------------------------------------------------------

def test_log_dispatch_first_call_retry_zero(conn):
    dispatch_id, retry_count = ledger.log_dispatch(conn, "fix-bug-42", "fork", "sonnet", "investigate bug")
    assert isinstance(dispatch_id, int)
    assert retry_count == 0


def test_log_dispatch_retry_count_increments(conn):
    _, r0 = ledger.log_dispatch(conn, "fix-bug-42", "fork", "sonnet", "attempt 1")
    _, r1 = ledger.log_dispatch(conn, "fix-bug-42", "fork", "sonnet", "attempt 2")
    _, r2 = ledger.log_dispatch(conn, "fix-bug-42", "fork", "sonnet", "attempt 3")
    assert (r0, r1, r2) == (0, 1, 2)


def test_log_dispatch_different_tasks_independent(conn):
    _, ra = ledger.log_dispatch(conn, "task-a", "fork", "sonnet", "a")
    _, rb = ledger.log_dispatch(conn, "task-b", "fork", "sonnet", "b")
    assert ra == 0
    assert rb == 0


def test_log_dispatch_rejects_empty_fields(conn):
    with pytest.raises(ValueError):
        ledger.log_dispatch(conn, "", "fork", "sonnet", "summary")
    with pytest.raises(ValueError):
        ledger.log_dispatch(conn, "task", "", "sonnet", "summary")
    with pytest.raises(ValueError):
        ledger.log_dispatch(conn, "task", "fork", "", "summary")
    with pytest.raises(ValueError):
        ledger.log_dispatch(conn, "task", "fork", "sonnet", "")


# --- log_verdict -----------------------------------------------------------

def test_log_verdict_attaches_and_returns_task(conn):
    dispatch_id, _ = ledger.log_dispatch(conn, "task-x", "fork", "sonnet", "summary")
    task = ledger.log_verdict(conn, dispatch_id, "CONFIRMED", notes="looks good")
    assert task == "task-x"
    row = conn.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    assert row["verdict"] == "CONFIRMED"
    assert row["verdict_notes"] == "looks good"
    assert row["verdict_ts"] is not None


def test_log_verdict_invalid_dispatch_id_raises(conn):
    with pytest.raises(ValueError, match="No dispatch found"):
        ledger.log_verdict(conn, 99999, "CONFIRMED")


def test_log_verdict_invalid_verdict_raises(conn):
    dispatch_id, _ = ledger.log_dispatch(conn, "task-y", "fork", "sonnet", "summary")
    with pytest.raises(ValueError, match="Invalid verdict"):
        ledger.log_verdict(conn, dispatch_id, "MAYBE")


def test_log_verdict_notes_optional(conn):
    dispatch_id, _ = ledger.log_dispatch(conn, "task-z", "fork", "sonnet", "summary")
    ledger.log_verdict(conn, dispatch_id, "REFUTED")
    row = conn.execute("SELECT * FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    assert row["verdict"] == "REFUTED"
    assert row["verdict_notes"] is None


# --- task_status -----------------------------------------------------------

def test_task_status_no_dispatches(conn):
    status = ledger.task_status(conn, "never-dispatched")
    assert status == {
        "dispatch_count": 0,
        "retry_count": 0,
        "last_verdict": None,
        "over_retry_limit": False,
    }


def test_task_status_tracks_retry_and_verdict(conn):
    d1, _ = ledger.log_dispatch(conn, "budget-task", "fork", "sonnet", "attempt 1")
    status = ledger.task_status(conn, "budget-task")
    assert status["dispatch_count"] == 1
    assert status["retry_count"] == 0
    assert status["over_retry_limit"] is False

    ledger.log_verdict(conn, d1, "REFUTED")
    status = ledger.task_status(conn, "budget-task")
    assert status["last_verdict"] == "REFUTED"


def test_task_status_over_retry_limit_flips_at_two(conn):
    ledger.log_dispatch(conn, "retry-task", "fork", "sonnet", "attempt 1")
    status = ledger.task_status(conn, "retry-task")
    assert status["retry_count"] == 0
    assert status["over_retry_limit"] is False

    ledger.log_dispatch(conn, "retry-task", "fork", "sonnet", "attempt 2")
    status = ledger.task_status(conn, "retry-task")
    assert status["retry_count"] == 1
    assert status["over_retry_limit"] is False

    ledger.log_dispatch(conn, "retry-task", "fork", "sonnet", "attempt 3")
    status = ledger.task_status(conn, "retry-task")
    assert status["retry_count"] == 2
    assert status["over_retry_limit"] is True

    ledger.log_dispatch(conn, "retry-task", "fork", "sonnet", "attempt 4")
    status = ledger.task_status(conn, "retry-task")
    assert status["retry_count"] == 3
    assert status["over_retry_limit"] is True


def test_task_status_last_verdict_is_most_recent(conn):
    d1, _ = ledger.log_dispatch(conn, "multi-task", "fork", "sonnet", "attempt 1")
    d2, _ = ledger.log_dispatch(conn, "multi-task", "fork", "sonnet", "attempt 2")
    ledger.log_verdict(conn, d1, "REFUTED")
    ledger.log_verdict(conn, d2, "CONFIRMED")
    status = ledger.task_status(conn, "multi-task")
    assert status["last_verdict"] == "CONFIRMED"


# --- session_report ----------------------------------------------------

def test_session_report_no_entries(conn):
    report = ledger.session_report(conn, since_hours=24)
    assert "no entries" in report.lower()


def test_session_report_contains_logged_action(conn):
    ledger.log_action(conn, 2, "deploy", "shipped v5", "IDFU")
    report = ledger.session_report(conn, since_hours=24)
    assert "自律実行リスト" in report
    assert "deploy" in report
    assert "[Tier 2] shipped v5 (IDFU)" in report


def test_session_report_contains_dispatch_with_pending_verdict(conn):
    ledger.log_dispatch(conn, "report-task", "fork", "sonnet", "summary")
    report = ledger.session_report(conn, since_hours=24)
    assert "## Dispatches" in report
    assert "report-task → fork/sonnet → PENDING" in report


def test_session_report_contains_dispatch_with_resolved_verdict(conn):
    dispatch_id, _ = ledger.log_dispatch(conn, "resolved-task", "fork", "opus", "summary")
    ledger.log_verdict(conn, dispatch_id, "CONFIRMED")
    report = ledger.session_report(conn, since_hours=24)
    assert "resolved-task → fork/opus → CONFIRMED" in report


def test_session_report_groups_actions_by_category(conn):
    ledger.log_action(conn, 0, "catA", "first")
    ledger.log_action(conn, 0, "catB", "second")
    ledger.log_action(conn, 0, "catA", "third")
    report = ledger.session_report(conn, since_hours=24)
    assert "### catA" in report
    assert "### catB" in report


def test_session_report_excludes_old_entries(conn):
    # Insert an action with a timestamp far in the past directly.
    conn.execute(
        "INSERT INTO actions (ts, tier, category, description, project) "
        "VALUES (datetime('now', '-100 hours'), 1, 'old', 'ancient action', NULL)"
    )
    conn.commit()
    report = ledger.session_report(conn, since_hours=24)
    assert "ancient action" not in report


def test_session_report_rejects_nonpositive_hours(conn):
    with pytest.raises(ValueError):
        ledger.session_report(conn, since_hours=0)


# --- stats ---------------------------------------------------------------

def test_stats_empty(conn):
    result = ledger.stats(conn, days=7)
    assert result["actions_by_tier"] == {"0": 0, "1": 0, "2": 0}
    assert result["actions_by_category"] == {}
    assert result["dispatch_count"] == 0
    assert result["verdicts"] == {
        "CONFIRMED": 0,
        "REFUTED": 0,
        "PARTIAL": 0,
        "PENDING": 0,
    }


def test_stats_counts_actions_by_tier_and_category(conn):
    ledger.log_action(conn, 0, "cat1", "a")
    ledger.log_action(conn, 1, "cat1", "b")
    ledger.log_action(conn, 1, "cat2", "c")
    ledger.log_action(conn, 2, "cat2", "d")
    result = ledger.stats(conn, days=7)
    assert result["actions_by_tier"] == {"0": 1, "1": 2, "2": 1}
    assert result["actions_by_category"] == {"cat1": 2, "cat2": 2}


def test_stats_counts_dispatches_and_verdicts(conn):
    d1, _ = ledger.log_dispatch(conn, "t1", "fork", "sonnet", "s")
    d2, _ = ledger.log_dispatch(conn, "t2", "fork", "sonnet", "s")
    d3, _ = ledger.log_dispatch(conn, "t3", "fork", "sonnet", "s")
    ledger.log_verdict(conn, d1, "CONFIRMED")
    ledger.log_verdict(conn, d2, "REFUTED")
    # d3 left PENDING

    result = ledger.stats(conn, days=7)
    assert result["dispatch_count"] == 3
    assert result["verdicts"] == {
        "CONFIRMED": 1,
        "REFUTED": 1,
        "PARTIAL": 0,
        "PENDING": 1,
    }


def test_stats_excludes_entries_outside_window(conn):
    conn.execute(
        "INSERT INTO actions (ts, tier, category, description, project) "
        "VALUES (datetime('now', '-30 days'), 0, 'old', 'ancient', NULL)"
    )
    conn.commit()
    result = ledger.stats(conn, days=7)
    assert result["actions_by_category"] == {}


def test_stats_rejects_nonpositive_days(conn):
    with pytest.raises(ValueError):
        ledger.stats(conn, days=0)
