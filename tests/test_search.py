"""Tests for search/similar_tasks/timeline in ledger.py — pure logic, no MCP involved.

Each test points NAGI_LEDGER_DB at a fresh tmp_path file so tests never
touch the real ledger DB.

The similar_tasks acceptance case (test_similar_tasks_catches_real_youtube_duplicate)
reproduces an actual production incident: two subagents were dispatched
50s apart for the same underlying task under different wording, and the
exact-match retry guard (ledger_task_status) missed it because the task
strings differed. See dispatches #82/#83 in ~/.nagi/ledger.db.
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


# --- search ------------------------------------------------------------


def test_search_finds_action_by_description(conn):
    ledger.log_action(conn, 1, "refactor", "cleaned up server.py", "nagi-ledger")
    results = ledger.search(conn, "cleaned server")
    assert len(results) == 1
    assert results[0]["kind"] == "actions"
    assert results[0]["title"] == "cleaned up server.py"


def test_search_finds_dispatch_by_task(conn):
    ledger.log_dispatch(conn, "fix-bug-42", "fork", "sonnet", "investigate the bug")
    results = ledger.search(conn, "fix bug")
    assert len(results) == 1
    assert results[0]["kind"] == "dispatches"
    assert results[0]["title"] == "fix-bug-42"


def test_search_finds_approach_by_task(conn):
    ledger.log_approach(conn, "flaky-test-fix", "add sleep(1)", "DEAD_END", "still flaky")
    results = ledger.search(conn, "flaky test")
    assert len(results) == 1
    assert results[0]["kind"] == "approaches"


def test_search_returns_index_rows_only_no_body_text(conn):
    """★ acceptance: search must never leak brief_summary/verdict_notes/reason."""
    ledger.log_dispatch(conn, "secret-task", "fork", "sonnet", "SENSITIVE_BODY_TEXT_MUST_NOT_LEAK")
    ledger.log_approach(
        conn, "secret-task-2", "some approach", "DEAD_END", "SECRET_REASON_MUST_NOT_LEAK"
    )
    results = ledger.search(conn, "secret")
    assert len(results) >= 2
    for row in results:
        assert set(row.keys()) == {"id", "kind", "ts", "title", "status"}
        for value in row.values():
            if isinstance(value, str):
                assert "SENSITIVE_BODY_TEXT_MUST_NOT_LEAK" not in value
                assert "SECRET_REASON_MUST_NOT_LEAK" not in value


def test_search_japanese_query_matches(conn):
    """★ acceptance: Japanese search must work (no whitespace word boundaries)."""
    ledger.log_dispatch(
        conn, "大規模ログ2(5MB)のgrep+サンプリング調査", "fork", "sonnet", "log audit"
    )
    results = ledger.search(conn, "大規模ログのサンプリング調査")
    assert len(results) == 1
    assert results[0]["title"] == "大規模ログ2(5MB)のgrep+サンプリング調査"


def test_search_kinds_filter(conn):
    ledger.log_action(conn, 0, "cat", "widget audit report")
    ledger.log_dispatch(conn, "widget audit task", "fork", "sonnet", "s")
    results_all = ledger.search(conn, "widget audit")
    assert {r["kind"] for r in results_all} == {"actions", "dispatches"}

    results_actions_only = ledger.search(conn, "widget audit", kinds=["actions"])
    assert {r["kind"] for r in results_actions_only} == {"actions"}


def test_search_rejects_unknown_kind(conn):
    with pytest.raises(ValueError):
        ledger.search(conn, "query", kinds=["bogus"])


def test_search_limit(conn):
    for i in range(5):
        ledger.log_action(conn, 0, "cat", f"widget report {i}")
    results = ledger.search(conn, "widget", limit=2)
    assert len(results) == 2


def test_search_offset(conn):
    for i in range(5):
        ledger.log_action(conn, 0, "cat", f"widget report {i}")
    page1 = ledger.search(conn, "widget", limit=2, offset=0)
    page2 = ledger.search(conn, "widget", limit=2, offset=2)
    ids1 = {r["id"] for r in page1}
    ids2 = {r["id"] for r in page2}
    assert ids1.isdisjoint(ids2)


def test_search_since_days_excludes_old_entries(conn):
    conn.execute(
        "INSERT INTO actions (ts, tier, category, description, project) "
        "VALUES (datetime('now', '-30 days'), 0, 'old', 'ancient widget report', NULL)"
    )
    conn.commit()
    ledger.log_action(conn, 0, "cat", "fresh widget report")

    results = ledger.search(conn, "widget", since_days=7)
    assert len(results) == 1
    assert results[0]["title"] == "fresh widget report"

    results_all = ledger.search(conn, "widget")
    assert len(results_all) == 2


def test_search_rejects_empty_query(conn):
    with pytest.raises(ValueError):
        ledger.search(conn, "")


def test_search_rejects_bad_limit(conn):
    with pytest.raises(ValueError):
        ledger.search(conn, "q", limit=0)


def test_search_rejects_bad_offset(conn):
    with pytest.raises(ValueError):
        ledger.search(conn, "q", offset=-1)


def test_search_rejects_bad_since_days(conn):
    with pytest.raises(ValueError):
        ledger.search(conn, "q", since_days=0)


def test_search_no_matches_returns_empty(conn):
    ledger.log_action(conn, 0, "cat", "totally unrelated content")
    assert ledger.search(conn, "zzz nonexistent xyz") == []


# --- similar_tasks -------------------------------------------------------


def test_similar_tasks_catches_real_youtube_duplicate(conn):
    """★ acceptance: reproduces the real production near-duplicate dispatch.

    #82  'Build the YouTube knowledge intake PoC'   nagi-implementer
    #83  'Build YouTube-to-Markdown intake PoC'      general-purpose  (50s later)

    task_status("Build YouTube-to-Markdown intake PoC") would return
    retry_count=0 because the string differs — similar_tasks must catch it.
    """
    ledger.log_dispatch(
        conn,
        "Build the YouTube knowledge intake PoC",
        "nagi-implementer",
        "sonnet",
        "PoC for YouTube transcript ingest",
    )
    # Distractor tasks that share a prefix but are NOT the same work —
    # these must NOT be mistaken for the duplicate.
    ledger.log_dispatch(conn, "Build accounts domain and fake provider", "fork", "sonnet", "s")
    ledger.log_dispatch(conn, "Build the tana CLI", "fork", "sonnet", "s")

    results = ledger.similar_tasks(conn, "Build YouTube-to-Markdown intake PoC")

    tasks_found = [r["task"] for r in results]
    assert "Build the YouTube knowledge intake PoC" in tasks_found

    match = next(r for r in results if r["task"] == "Build the YouTube knowledge intake PoC")
    assert match["kind"] == "dispatches"
    assert match["score"] >= 0.35
    # Distractors must not appear at all in the returned (>= min_score) set.
    assert "Build accounts domain and fake provider" not in tasks_found
    assert "Build the tana CLI" not in tasks_found


def test_similar_tasks_no_false_positive_for_unrelated_task(conn):
    """★ acceptance: unrelated task strings must NOT be reported as similar."""
    ledger.log_dispatch(conn, "Build the YouTube knowledge intake PoC", "fork", "sonnet", "s")
    ledger.log_dispatch(conn, "Fix flaky test in dispatch_guard", "fork", "sonnet", "s")
    ledger.log_approach(
        conn,
        "Investigate memory leak in session_brief",
        "profile with tracemalloc",
        "WORKS",
        "found it",
    )

    results = ledger.similar_tasks(conn, "Refactor the invoicing export CSV writer")
    assert results == []


def test_similar_tasks_matches_approaches_table_too(conn):
    ledger.log_approach(
        conn, "Build the YouTube knowledge intake PoC", "used yt-dlp", "WORKS", "worked fine"
    )
    results = ledger.similar_tasks(conn, "Build YouTube-to-Markdown intake PoC")
    assert any(r["kind"] == "approaches" for r in results)


def test_similar_tasks_sorted_by_score_desc(conn):
    ledger.log_dispatch(conn, "Build the YouTube knowledge intake PoC", "fork", "sonnet", "s")
    ledger.log_dispatch(conn, "Build YouTube to Markdown PoC intake", "fork", "sonnet", "s")
    results = ledger.similar_tasks(conn, "Build YouTube-to-Markdown intake PoC")
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_similar_tasks_respects_limit(conn):
    for i in range(10):
        ledger.log_dispatch(conn, f"Build YouTube intake PoC variant {i}", "fork", "sonnet", "s")
    results = ledger.similar_tasks(
        conn, "Build YouTube intake PoC variant X", limit=3, min_score=0.1
    )
    assert len(results) <= 3


def test_similar_tasks_verdict_field_reflects_kind(conn):
    d_id, _ = ledger.log_dispatch(
        conn, "Build the YouTube knowledge intake PoC", "fork", "sonnet", "s"
    )
    ledger.log_verdict(conn, d_id, "CONFIRMED")
    results = ledger.similar_tasks(conn, "Build YouTube-to-Markdown intake PoC")
    match = next(r for r in results if r["kind"] == "dispatches")
    assert match["verdict"] == "CONFIRMED"


def test_similar_tasks_rejects_empty_task(conn):
    with pytest.raises(ValueError):
        ledger.similar_tasks(conn, "")


def test_similar_tasks_rejects_bad_limit(conn):
    with pytest.raises(ValueError):
        ledger.similar_tasks(conn, "task", limit=0)


def test_similar_tasks_empty_db_returns_empty(conn):
    assert ledger.similar_tasks(conn, "anything") == []


# --- timeline --------------------------------------------------------------


def _insert_action(conn, ts, description):
    cur = conn.execute(
        "INSERT INTO actions (ts, tier, category, description, project) VALUES (?, 0, 'cat', ?, NULL)",
        (ts, description),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_dispatch(conn, ts, task):
    cur = conn.execute(
        "INSERT INTO dispatches (ts, task, agent_type, model, brief_summary) "
        "VALUES (?, ?, 'fork', 'sonnet', 's')",
        (ts, task),
    )
    conn.commit()
    return int(cur.lastrowid)


def _insert_approach(conn, ts, task):
    cur = conn.execute(
        "INSERT INTO approaches (ts, task, approach, outcome, reason) "
        "VALUES (?, ?, 'a', 'WORKS', 'r')",
        (ts, task),
    )
    conn.commit()
    return int(cur.lastrowid)


def test_timeline_returns_anchor_marked(conn):
    # Explicit, strictly-increasing timestamps: sqlite datetime('now') only
    # has 1-second resolution, and inserts within one test run easily land
    # in the same second, which would make ordering ambiguous.
    _insert_action(conn, "2026-01-01 00:00:01", "before")
    dispatch_id = _insert_dispatch(conn, "2026-01-01 00:00:02", "anchor-task")
    _insert_action(conn, "2026-01-01 00:00:03", "after")

    results = ledger.timeline(conn, "dispatches", dispatch_id, before=1, after=1)
    anchors = [r for r in results if r["anchor"]]
    assert len(anchors) == 1
    assert anchors[0]["id"] == dispatch_id
    assert anchors[0]["kind"] == "dispatches"


def test_timeline_before_after_counts(conn):
    for i in range(3):
        _insert_action(conn, f"2026-01-01 00:00:{i:02d}", f"action-{i}")
    dispatch_id = _insert_dispatch(conn, "2026-01-01 00:00:10", "mid-task")
    for i in range(3):
        _insert_action(conn, f"2026-01-01 00:00:{20 + i}", f"action-after-{i}")

    results = ledger.timeline(conn, "dispatches", dispatch_id, before=2, after=2)
    assert len(results) == 5  # 2 before + anchor + 2 after
    anchor_pos = next(i for i, r in enumerate(results) if r["anchor"])
    assert anchor_pos == 2  # exactly 2 rows before the anchor in the window


def test_timeline_clamps_at_start_of_data(conn):
    action_id = ledger.log_action(conn, 0, "cat", "only entry")
    results = ledger.timeline(conn, "actions", action_id, before=5, after=5)
    assert len(results) == 1
    assert results[0]["anchor"] is True


def test_timeline_unknown_anchor_kind_raises(conn):
    with pytest.raises(ValueError):
        ledger.timeline(conn, "bogus", 1)


def test_timeline_missing_anchor_id_raises(conn):
    with pytest.raises(ValueError):
        ledger.timeline(conn, "actions", 99999)


def test_timeline_negative_before_after_raises(conn):
    action_id = ledger.log_action(conn, 0, "cat", "entry")
    with pytest.raises(ValueError):
        ledger.timeline(conn, "actions", action_id, before=-1)
    with pytest.raises(ValueError):
        ledger.timeline(conn, "actions", action_id, after=-1)


def test_timeline_spans_multiple_tables(conn):
    ledger.log_action(conn, 0, "cat", "action-1")
    dispatch_id, _ = ledger.log_dispatch(conn, "task-1", "fork", "sonnet", "s")
    ledger.log_approach(conn, "task-2", "approach-1", "WORKS", "reason-1")

    results = ledger.timeline(conn, "dispatches", dispatch_id, before=5, after=5)
    kinds_present = {r["kind"] for r in results}
    assert "actions" in kinds_present
    assert "approaches" in kinds_present
