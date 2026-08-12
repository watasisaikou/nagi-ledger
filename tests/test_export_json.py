"""Tests for export_json.py — the read-only JSON export ledger-view reads.

The output shape (key names, types, array order) is a CONTRACT shared with
a sibling repo (github.com/watasisaikou/ledger-view) being built in
parallel, so these tests assert the contract directly rather than just
"it runs without crashing": the schema key, full row counts for all three
tables, both null and non-null verdict fields, a null project, and id
ascending order.

Everything below drives the script as an actual subprocess (`sys.executable`,
the interpreter running pytest) rather than importing and calling main() in
process — the CLI surface (argv parsing, stdout vs --out, exit codes) is
exactly what a real caller (ledger-view's build step, or a human) depends
on, and only a subprocess proves it end to end.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import ledger

REPO_ROOT = Path(__file__).resolve().parent.parent
PYTHON = sys.executable


def _run_export(db_path: Path, extra_args: list[str] | None = None):
    import os

    env = {**os.environ, "NAGI_LEDGER_DB": str(db_path)}
    args = [PYTHON, str(REPO_ROOT / "export_json.py"), *(extra_args or [])]
    return subprocess.run(
        args,
        capture_output=True,
        text=False,  # capture raw bytes so newline style is checkable
        cwd=str(REPO_ROOT),
        env=env,
    )


@pytest.fixture()
def seeded_db(tmp_path):
    """A ledger DB with rows covering every field variant the contract cares
    about: a null project, a null-verdict (unverified) dispatch, a
    non-null-verdict dispatch, and an approach — inserted deliberately
    out of id order relative to how they'll be asserted, so an accidental
    ORDER BY ts or insertion-order-of-dict rather than `id ASC` would show up.
    """
    db_path = tmp_path / "ledger.db"
    conn = ledger.get_connection(db_path)
    try:
        ledger.log_action(conn, 1, "cat-a", "action one", project="proj-x")
        ledger.log_action(conn, 0, "cat-b", "action two")  # project is None

        d1_id, _ = ledger.log_dispatch(conn, "task-unverified", "nagi-implementer", "sonnet", "b1")
        d2_id, _ = ledger.log_dispatch(conn, "task-verified", "nagi-verifier", "haiku", "b2")
        ledger.log_verdict(conn, d2_id, "CONFIRMED", "checked it")
        # d1 stays unverified: verdict/verdict_ts/verdict_notes must be null

        ledger.log_approach(conn, "task-approach", "tried X", "DEAD_END", "toolchain missing")
    finally:
        conn.close()
    return db_path, {"d1_id": d1_id, "d2_id": d2_id}


# --- contract shape ---------------------------------------------------------


def test_schema_key_and_top_level_shape(seeded_db):
    db_path, _ = seeded_db
    result = _run_export(db_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)

    assert data["schema"] == "nagi-ledger-export/1"
    assert set(data.keys()) == {"schema", "exported_at", "actions", "dispatches", "approaches"}
    # exported_at matches the ledger's own ts format: "YYYY-MM-DD HH:MM:SS"
    assert len(data["exported_at"]) == 19
    assert data["exported_at"][4] == "-" and data["exported_at"][10] == " "


def test_all_three_arrays_present_full_and_id_ascending(seeded_db):
    db_path, ids = seeded_db
    result = _run_export(db_path)
    data = json.loads(result.stdout)

    assert len(data["actions"]) == 2
    assert len(data["dispatches"]) == 2
    assert len(data["approaches"]) == 1

    assert [row["id"] for row in data["actions"]] == sorted(row["id"] for row in data["actions"])
    assert [row["id"] for row in data["dispatches"]] == [ids["d1_id"], ids["d2_id"]]
    assert [row["id"] for row in data["approaches"]] == sorted(
        row["id"] for row in data["approaches"]
    )


def test_action_project_null_when_absent(seeded_db):
    db_path, _ = seeded_db
    result = _run_export(db_path)
    data = json.loads(result.stdout)

    by_desc = {row["description"]: row for row in data["actions"]}
    assert by_desc["action one"]["project"] == "proj-x"
    assert by_desc["action two"]["project"] is None


def test_dispatch_verdict_fields_null_when_unverified_and_set_when_verified(seeded_db):
    db_path, ids = seeded_db
    result = _run_export(db_path)
    data = json.loads(result.stdout)

    by_id = {row["id"]: row for row in data["dispatches"]}

    unverified = by_id[ids["d1_id"]]
    assert unverified["verdict"] is None
    assert unverified["verdict_ts"] is None
    assert unverified["verdict_notes"] is None

    verified = by_id[ids["d2_id"]]
    assert verified["verdict"] == "CONFIRMED"
    assert verified["verdict_ts"] is not None
    assert verified["verdict_notes"] == "checked it"


def test_dispatch_and_action_and_approach_field_sets(seeded_db):
    """Pins every key name in the contract, so a rename/drop is caught here
    rather than downstream in ledger-view."""
    db_path, _ = seeded_db
    result = _run_export(db_path)
    data = json.loads(result.stdout)

    assert set(data["actions"][0].keys()) == {
        "id",
        "ts",
        "tier",
        "category",
        "description",
        "project",
    }
    assert set(data["dispatches"][0].keys()) == {
        "id",
        "ts",
        "task",
        "agent_type",
        "model",
        "brief_summary",
        "verdict",
        "verdict_ts",
        "verdict_notes",
    }
    assert set(data["approaches"][0].keys()) == {
        "id",
        "ts",
        "task",
        "approach",
        "outcome",
        "reason",
    }


# --- CLI: --out writes UTF-8 with LF newlines -------------------------------


def test_out_flag_writes_utf8_lf_file(seeded_db, tmp_path):
    db_path, _ = seeded_db
    out_path = tmp_path / "export.json"

    result = _run_export(db_path, ["--out", str(out_path)])
    assert result.returncode == 0, result.stderr
    assert result.stdout == b""  # nothing on stdout when writing to a file

    raw = out_path.read_bytes()
    assert b"\r\n" not in raw, "must be LF, not CRLF, regardless of platform"
    text = raw.decode("utf-8")
    data = json.loads(text)
    assert data["schema"] == "nagi-ledger-export/1"


def test_stdout_mode_is_valid_json_and_non_ascii_survives(tmp_path):
    db_path = tmp_path / "ledger.db"
    conn = ledger.get_connection(db_path)
    try:
        ledger.log_action(conn, 1, "日本語カテゴリ", "日本語の説明")
    finally:
        conn.close()

    result = _run_export(db_path)
    assert result.returncode == 0, result.stderr
    text = result.stdout.decode("utf-8")
    data = json.loads(text)
    assert data["actions"][0]["description"] == "日本語の説明"
    # ensure_ascii=False: real characters on the wire, not \uXXXX escapes
    assert "\\u" not in text


# --- missing DB: clean failure, no DB created -------------------------------


def test_missing_db_fails_loudly_without_creating_one(tmp_path):
    db_path = tmp_path / "does_not_exist" / "ledger.db"
    result = _run_export(db_path)

    assert result.returncode == 1
    assert result.stdout == b""
    assert str(db_path) in result.stderr.decode("utf-8")
    assert not db_path.exists(), "a failed export must never create an empty DB"


def test_missing_db_via_explicit_db_flag(tmp_path):
    missing = tmp_path / "nope.db"
    real_db = tmp_path / "real.db"
    ledger.get_connection(real_db).close()

    # --db points at a nonexistent file even though NAGI_LEDGER_DB (unset
    # here, but a real one could exist) is not what's being checked.
    result = _run_export(real_db, ["--db", str(missing)])
    assert result.returncode == 1
    assert not missing.exists()


# --- bad arguments -----------------------------------------------------------


def test_unknown_flag_fails_with_nonzero_exit(tmp_path):
    db_path = tmp_path / "ledger.db"
    ledger.get_connection(db_path).close()

    result = _run_export(db_path, ["--bogus"])
    assert result.returncode == 1
    assert b"error" in result.stderr.lower() or b"error" in result.stderr


def test_out_flag_missing_value_fails(tmp_path):
    db_path = tmp_path / "ledger.db"
    ledger.get_connection(db_path).close()

    result = _run_export(db_path, ["--out"])
    assert result.returncode == 1


# --- read-only: never writes to the source DB -------------------------------


def _table_counts(db_path: Path) -> dict:
    conn = ledger.get_connection(db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("actions", "dispatches", "approaches")
        }
    finally:
        conn.close()


def test_export_does_not_modify_the_source_db(seeded_db):
    db_path, _ = seeded_db
    before = _table_counts(db_path)
    result = _run_export(db_path)
    assert result.returncode == 0, result.stderr
    after = _table_counts(db_path)
    assert before == after
