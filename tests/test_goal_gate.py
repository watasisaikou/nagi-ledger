"""Tests for goal_gate.py — a deterministic "keep working until done" gate
for Claude Code's Stop hook.

Two layers of tests:
  1. In-process, calling goal_gate.main()/cmd_* with NAGI_GOAL_FILE and
     NAGI_GOAL_HISTORY pointed at tmp_path — fast, exercises CLI + hook logic.
  2. Real subprocess end-to-end tests (system python) to prove the script
     behaves correctly as an actual Stop hook command: exit code 0 always,
     exact stdout shapes.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import goal_gate

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def goal_env(tmp_path, monkeypatch):
    goal_file = tmp_path / "goal.json"
    history_file = tmp_path / "goal_history.jsonl"
    monkeypatch.setenv("NAGI_GOAL_FILE", str(goal_file))
    monkeypatch.setenv("NAGI_GOAL_HISTORY", str(history_file))
    return {"goal_file": goal_file, "history_file": history_file}


def _read_history(history_file: Path) -> list[dict]:
    if not history_file.exists():
        return []
    lines = [ln for ln in history_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    return [json.loads(ln) for ln in lines]


def run_stop_gate(monkeypatch, capsys, stdin_text: str = "{}") -> tuple[int, str]:
    capsys.readouterr()  # discard any output buffered from prior calls (e.g. "set")
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(stdin_text))
    exit_code = goal_gate.main(["stop-gate"])
    out = capsys.readouterr().out
    return exit_code, out


# --- set / status ------------------------------------------------------


def test_set_then_status_shows_goal(goal_env, capsys):
    exit_code = goal_gate.main(["set", "ship the widget", "--max-turns", "5"])
    assert exit_code == 0

    capsys.readouterr()
    status_code = goal_gate.main(["status"])
    out = capsys.readouterr().out
    assert status_code == 0
    assert "ship the widget" in out
    assert "5" in out


def test_status_no_active_goal(goal_env, capsys):
    exit_code = goal_gate.main(["status"])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "no active goal" in out


def test_set_default_max_turns(goal_env):
    goal_gate.main(["set", "do the thing"])
    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["max_turns"] == 10
    assert state["remaining"] == 10


def test_set_rejects_out_of_range_max_turns(goal_env, capsys):
    exit_code = goal_gate.main(["set", "goal text", "--max-turns", "0"])
    assert exit_code == 1
    exit_code2 = goal_gate.main(["set", "goal text", "--max-turns", "101"])
    assert exit_code2 == 1
    assert not goal_env["goal_file"].exists()


def test_set_again_archives_old_goal_as_superseded(goal_env):
    goal_gate.main(["set", "first goal", "--max-turns", "3"])
    goal_gate.main(["set", "second goal", "--max-turns", "4"])

    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["goal"] == "second goal"
    assert state["max_turns"] == 4

    history = _read_history(goal_env["history_file"])
    assert len(history) == 1
    assert history[0]["goal"] == "first goal"
    assert history[0]["outcome"] == "superseded"


# --- stop-gate: no active goal ------------------------------------------


def test_stop_gate_no_goal_silent_exit_zero(goal_env, monkeypatch, capsys):
    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


# --- stop-gate: active goal, blocking ------------------------------------


def test_stop_gate_active_goal_blocks_and_decrements(goal_env, monkeypatch, capsys):
    goal_gate.main(["set", "finish the report", "--max-turns", "5"])

    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"
    assert "finish the report" in payload["reason"]
    assert "Turns left: 4" in payload["reason"]

    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["remaining"] == 4


def test_stop_gate_reason_uses_runtime_paths_not_hardcoded(goal_env, monkeypatch, capsys):
    """Regression: the reason used to embed a hardcoded personal path
    (C:/Dev/nagi-ledger-mcp/goal_gate.py). It must instead be derived at
    runtime from sys.executable and this file's actual location."""
    goal_gate.main(["set", "reason path check", "--max-turns", "5"])

    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    reason = json.loads(out)["reason"]

    assert "C:/Dev" not in reason
    assert sys.executable in reason
    assert str(Path(goal_gate.__file__).resolve()) in reason


def test_stop_gate_tolerates_garbage_stdin(goal_env, monkeypatch, capsys):
    goal_gate.main(["set", "goal x", "--max-turns", "5"])
    exit_code, out = run_stop_gate(monkeypatch, capsys, stdin_text="not json {{{")
    assert exit_code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"


def test_stop_gate_tolerates_empty_stdin(goal_env, monkeypatch, capsys):
    goal_gate.main(["set", "goal y", "--max-turns", "5"])
    exit_code, out = run_stop_gate(monkeypatch, capsys, stdin_text="")
    assert exit_code == 0
    payload = json.loads(out)
    assert payload["decision"] == "block"


# --- stop-gate: budget exhaustion -----------------------------------------


def test_stop_gate_budget_exhaustion(goal_env, monkeypatch, capsys):
    goal_gate.main(["set", "small budget goal", "--max-turns", "2"])

    exit_code1, out1 = run_stop_gate(monkeypatch, capsys)
    assert exit_code1 == 0
    payload1 = json.loads(out1)
    assert payload1["decision"] == "block"
    assert "Turns left: 1" in payload1["reason"]
    assert goal_env["goal_file"].exists()

    exit_code2, out2 = run_stop_gate(monkeypatch, capsys)
    assert exit_code2 == 0
    payload2 = json.loads(out2)
    assert "systemMessage" in payload2
    assert "budget exhausted" in payload2["systemMessage"]
    assert "small budget goal" in payload2["systemMessage"]
    assert "decision" not in payload2

    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert len(history) == 1
    assert history[0]["outcome"] == "budget_exhausted"
    assert history[0]["goal"] == "small budget goal"
    assert history[0]["turns_used"] == 2


# --- done / abort lifecycle ------------------------------------------------


def test_done_archives_and_clears_state(goal_env, capsys):
    goal_gate.main(["set", "a goal to finish", "--max-turns", "5"])
    capsys.readouterr()

    exit_code = goal_gate.main(["done", "shipped and verified"])
    assert exit_code == 0
    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert len(history) == 1
    assert history[0]["outcome"] == "done"
    assert history[0]["note"] == "shipped and verified"
    assert history[0]["goal"] == "a goal to finish"
    assert "closed" in history[0]


def test_abort_archives_and_clears_state(goal_env):
    goal_gate.main(["set", "a goal to abandon", "--max-turns", "5"])
    exit_code = goal_gate.main(["abort", "blocked by external dependency"])
    assert exit_code == 0
    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert len(history) == 1
    assert history[0]["outcome"] == "aborted"
    assert history[0]["note"] == "blocked by external dependency"


def test_done_with_no_active_goal_errors(goal_env, capsys):
    exit_code = goal_gate.main(["done"])
    assert exit_code == 1


def test_abort_with_no_active_goal_errors(goal_env, capsys):
    exit_code = goal_gate.main(["abort"])
    assert exit_code == 1


def test_done_after_done_errors(goal_env):
    goal_gate.main(["set", "goal z", "--max-turns", "5"])
    assert goal_gate.main(["done"]) == 0
    assert goal_gate.main(["done"]) == 1


def test_stop_gate_after_done_is_silent(goal_env, monkeypatch, capsys):
    goal_gate.main(["set", "goal to finish then check", "--max-turns", "5"])
    capsys.readouterr()
    goal_gate.main(["done", "evidence here"])
    capsys.readouterr()

    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""


# --- corrupt state file ---------------------------------------------------


def test_stop_gate_corrupt_state_file(goal_env, monkeypatch, capsys):
    goal_env["goal_file"].parent.mkdir(parents=True, exist_ok=True)
    goal_env["goal_file"].write_text("{not valid json", encoding="utf-8")

    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""
    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert len(history) == 1
    assert history[0]["outcome"] == "corrupt_state"


def test_stop_gate_state_file_missing_required_keys(goal_env, monkeypatch, capsys):
    goal_env["goal_file"].parent.mkdir(parents=True, exist_ok=True)
    goal_env["goal_file"].write_text(json.dumps({"goal": "incomplete"}), encoding="utf-8")

    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    assert out == ""
    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert history[-1]["outcome"] == "corrupt_state"


# --- end-to-end subprocess (system python) ---------------------------------


def _run_subprocess(python_exe: str, args: list[str], env: dict, stdin_text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [python_exe, str(REPO_ROOT / "goal_gate.py"), *args],
        input=stdin_text,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


def test_end_to_end_subprocess_full_cycle(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    goal_file = tmp_path / "e2e_goal.json"
    history_file = tmp_path / "e2e_history.jsonl"
    env = {
        **__import__("os").environ,
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_GOAL_HISTORY": str(history_file),
    }

    # 1. set
    result_set = _run_subprocess(system_python, ["set", "e2e goal", "--max-turns", "3"], env)
    assert result_set.returncode == 0, f"stderr={result_set.stderr!r}"
    assert goal_file.exists()

    # 2. stop-gate -> block
    result_gate1 = _run_subprocess(system_python, ["stop-gate"], env, stdin_text=json.dumps({"session_id": "s1"}))
    assert result_gate1.returncode == 0, f"stderr={result_gate1.stderr!r}"
    payload1 = json.loads(result_gate1.stdout)
    assert payload1["decision"] == "block"
    assert "e2e goal" in payload1["reason"]
    assert "Turns left: 2" in payload1["reason"]

    # 3. done
    result_done = _run_subprocess(system_python, ["done", "verified via e2e test"], env)
    assert result_done.returncode == 0, f"stderr={result_done.stderr!r}"
    assert not goal_file.exists()

    # 4. stop-gate -> silent
    result_gate2 = _run_subprocess(system_python, ["stop-gate"], env, stdin_text="{}")
    assert result_gate2.returncode == 0, f"stderr={result_gate2.stderr!r}"
    assert result_gate2.stdout == ""

    history = _read_history(history_file)
    assert len(history) == 1
    assert history[0]["outcome"] == "done"
    assert history[0]["note"] == "verified via e2e test"


def test_end_to_end_subprocess_stop_gate_never_crashes_on_garbage(tmp_path):
    system_python = shutil.which("python") or shutil.which("python3")
    if not system_python:
        pytest.skip("system python interpreter not found")

    goal_file = tmp_path / "e2e_goal2.json"
    history_file = tmp_path / "e2e_history2.jsonl"
    env = {
        **__import__("os").environ,
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_GOAL_HISTORY": str(history_file),
    }

    result = _run_subprocess(system_python, ["stop-gate"], env, stdin_text="totally not json {{{")
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""
