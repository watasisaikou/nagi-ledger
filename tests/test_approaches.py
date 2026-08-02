"""Tests for the approaches (dead-end registry) part of ledger.py — pure logic, no MCP involved.

Each test points NAGI_LEDGER_DB at a fresh tmp_path file so tests never
touch the real ledger DB.
"""

from __future__ import annotations

import pytest

import ledger


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    db_path = tmp_path / "ledger_test.db"
    monkeypatch.setenv("NAGI_LEDGER_DB", str(db_path))
    connection = ledger.get_connection()
    yield connection
    connection.close()


def test_schema_creates_approaches_table(conn):
    tables = {
        row["name"]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "approaches" in tables


# --- log_approach validation -----------------------------------------------


def test_log_approach_success(conn):
    approach_id = ledger.log_approach(
        conn, "fix-flaky-test", "add sleep(1)", "DEAD_END", "still flaky, race condition"
    )
    assert isinstance(approach_id, int)
    row = conn.execute("SELECT * FROM approaches WHERE id = ?", (approach_id,)).fetchone()
    assert row["task"] == "fix-flaky-test"
    assert row["approach"] == "add sleep(1)"
    assert row["outcome"] == "DEAD_END"
    assert row["reason"] == "still flaky, race condition"


def test_log_approach_rejects_bad_outcome(conn):
    with pytest.raises(ValueError) as excinfo:
        ledger.log_approach(conn, "task", "approach", "MAYBE", "reason")
    msg = str(excinfo.value)
    assert "DEAD_END" in msg
    assert "NO_GO" in msg
    assert "WORKS" in msg


@pytest.mark.parametrize("bad_outcome", ["", None, "dead_end", "SUCCESS"])
def test_log_approach_rejects_various_bad_outcomes(conn, bad_outcome):
    with pytest.raises(ValueError):
        ledger.log_approach(conn, "task", "approach", bad_outcome, "reason")


def test_log_approach_rejects_empty_task(conn):
    with pytest.raises(ValueError):
        ledger.log_approach(conn, "", "approach", "DEAD_END", "reason")


def test_log_approach_rejects_empty_approach(conn):
    with pytest.raises(ValueError):
        ledger.log_approach(conn, "task", "   ", "DEAD_END", "reason")


def test_log_approach_rejects_empty_reason(conn):
    with pytest.raises(ValueError):
        ledger.log_approach(conn, "task", "approach", "DEAD_END", "")


# --- log_approach -> check_approaches round-trip ----------------------------


def test_round_trip_dead_end(conn):
    ledger.log_approach(conn, "task-a", "approach-1", "DEAD_END", "reason-1")
    result = ledger.check_approaches(conn, "task-a")
    assert result["task"] == "task-a"
    assert result["total"] == 1
    assert result["dead_ends"] == [{"approach": "approach-1", "reason": "reason-1", "ts": result["dead_ends"][0]["ts"]}]
    assert result["no_gos"] == []
    assert result["works"] == []


def test_round_trip_no_go(conn):
    ledger.log_approach(conn, "task-b", "approach-2", "NO_GO", "reason-2")
    result = ledger.check_approaches(conn, "task-b")
    assert result["total"] == 1
    assert result["no_gos"][0]["approach"] == "approach-2"
    assert result["no_gos"][0]["reason"] == "reason-2"
    assert result["dead_ends"] == []
    assert result["works"] == []


def test_round_trip_works(conn):
    ledger.log_approach(conn, "task-c", "approach-3", "WORKS", "reason-3")
    result = ledger.check_approaches(conn, "task-c")
    assert result["total"] == 1
    assert result["works"][0]["approach"] == "approach-3"
    assert result["works"][0]["reason"] == "reason-3"
    assert result["dead_ends"] == []
    assert result["no_gos"] == []


# --- ordering ----------------------------------------------------------------


def test_ordering_newest_first(conn):
    ledger.log_approach(conn, "task-order", "first", "DEAD_END", "r1")
    ledger.log_approach(conn, "task-order", "second", "DEAD_END", "r2")
    ledger.log_approach(conn, "task-order", "third", "DEAD_END", "r3")
    result = ledger.check_approaches(conn, "task-order")
    approaches = [item["approach"] for item in result["dead_ends"]]
    assert approaches == ["third", "second", "first"]


# --- multiple tasks isolated --------------------------------------------------


def test_multiple_tasks_isolated(conn):
    ledger.log_approach(conn, "task-x", "x-approach", "DEAD_END", "x-reason")
    ledger.log_approach(conn, "task-y", "y-approach", "NO_GO", "y-reason")

    result_x = ledger.check_approaches(conn, "task-x")
    assert result_x["total"] == 1
    assert result_x["dead_ends"][0]["approach"] == "x-approach"
    assert result_x["no_gos"] == []

    result_y = ledger.check_approaches(conn, "task-y")
    assert result_y["total"] == 1
    assert result_y["no_gos"][0]["approach"] == "y-approach"
    assert result_y["dead_ends"] == []


def test_mixed_outcomes_bucketed_correctly(conn):
    ledger.log_approach(conn, "task-mixed", "a1", "DEAD_END", "r1")
    ledger.log_approach(conn, "task-mixed", "a2", "NO_GO", "r2")
    ledger.log_approach(conn, "task-mixed", "a3", "WORKS", "r3")
    result = ledger.check_approaches(conn, "task-mixed")
    assert result["total"] == 3
    assert len(result["dead_ends"]) == 1
    assert len(result["no_gos"]) == 1
    assert len(result["works"]) == 1


# --- unknown task --------------------------------------------------------------


def test_check_unknown_task_all_empty(conn):
    result = ledger.check_approaches(conn, "never-logged-task")
    assert result == {
        "task": "never-logged-task",
        "dead_ends": [],
        "no_gos": [],
        "works": [],
        "total": 0,
    }


def test_check_approaches_rejects_empty_task(conn):
    with pytest.raises(ValueError):
        ledger.check_approaches(conn, "")
