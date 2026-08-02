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


# --- CLI usage / help -----------------------------------------------------


def test_no_args_prints_usage_to_stderr_exit_1(capsys):
    exit_code = goal_gate.main([])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "usage: goal_gate.py" in captured.err
    assert "extend" in captured.err


def test_unknown_command_prints_error_exit_1(capsys):
    exit_code = goal_gate.main(["bogus-command"])
    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "unknown command" in captured.err


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_flag_prints_usage_exit_0(capsys, flag):
    exit_code = goal_gate.main([flag])
    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "usage: goal_gate.py" in captured.out
    assert "extend" in captured.out


# --- extend --------------------------------------------------------------


def test_extend_increases_remaining_and_max_turns(goal_env):
    """extend must add its amount to BOTH remaining and max_turns (not just
    remaining) and record a single extension."""
    goal_gate.main(["set", "extend basic goal", "--max-turns", "3"])
    exit_code = goal_gate.main(["extend", "5"])
    assert exit_code == 0

    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["remaining"] == 8
    assert state["max_turns"] == 8
    assert state["extensions"] == 1


def test_extend_twice_increments_extensions_and_adds_again(goal_env):
    """A second extend must accumulate on top of the first, not replace it,
    and extensions must count total extend calls."""
    goal_gate.main(["set", "extend twice goal", "--max-turns", "3"])
    goal_gate.main(["extend", "5"])
    exit_code = goal_gate.main(["extend", "4"])
    assert exit_code == 0

    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["remaining"] == 12  # 3 + 5 + 4
    assert state["max_turns"] == 12
    assert state["extensions"] == 2


@pytest.mark.parametrize(
    "extend_args",
    [["extend", "0"], ["extend", "101"], ["extend", "abc"], ["extend"]],
    ids=["zero", "over-max", "non-integer", "missing-arg"],
)
def test_extend_rejects_invalid_amount_without_mutating_state(goal_env, extend_args):
    """Regression: a rejected extend (out-of-range, non-integer, or missing
    amount) must not partially write the state file — every invalid-amount
    path must be a pure no-op on disk, proven by comparing raw bytes."""
    goal_gate.main(["set", "goal to protect", "--max-turns", "5"])
    bytes_before = goal_env["goal_file"].read_bytes()

    exit_code = goal_gate.main(extend_args)
    assert exit_code == 1

    bytes_after = goal_env["goal_file"].read_bytes()
    assert bytes_after == bytes_before


def test_extend_with_no_active_goal_errors_and_creates_no_file(goal_env):
    """extend must fail (not silently create a goal) when there is nothing
    active to extend, and must not create a state file as a side effect."""
    exit_code = goal_gate.main(["extend", "5"])
    assert exit_code == 1
    assert not goal_env["goal_file"].exists()


def test_extend_corrupt_state_non_numeric_remaining_errors_no_mutation(goal_env, capsys):
    """A state file with a non-numeric `remaining` must be reported as
    corrupt (not crash, not silently coerce) and must not be rewritten."""
    goal_env["goal_file"].parent.mkdir(parents=True, exist_ok=True)
    corrupt = {
        "goal": "corrupt goal",
        "remaining": "not-a-number",
        "max_turns": 5,
        "created": "2026-01-01T00:00:00+00:00",
    }
    goal_env["goal_file"].write_text(json.dumps(corrupt), encoding="utf-8")
    bytes_before = goal_env["goal_file"].read_bytes()

    exit_code = goal_gate.main(["extend", "5"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "corrupt goal state" in err
    assert goal_env["goal_file"].read_bytes() == bytes_before


def test_extend_corrupt_state_none_remaining_errors_no_mutation(goal_env, capsys):
    """Same corrupt-state contract as above, for the `remaining: null`
    shape specifically (int(None) raises TypeError, a different code path
    than int("not-a-number")'s ValueError — both must be handled)."""
    goal_env["goal_file"].parent.mkdir(parents=True, exist_ok=True)
    corrupt = {
        "goal": "corrupt goal null remaining",
        "remaining": None,
        "max_turns": 5,
        "created": "2026-01-01T00:00:00+00:00",
    }
    goal_env["goal_file"].write_text(json.dumps(corrupt), encoding="utf-8")
    bytes_before = goal_env["goal_file"].read_bytes()

    exit_code = goal_gate.main(["extend", "5"])
    err = capsys.readouterr().err
    assert exit_code == 1
    assert "corrupt goal state" in err
    assert goal_env["goal_file"].read_bytes() == bytes_before


def test_extend_with_already_negative_remaining_arithmetic(goal_env):
    """If remaining somehow went negative already (e.g. a race with
    stop-gate), extend must not crash and must apply plain addition — this
    pins the exact arithmetic rather than just "doesn't raise"."""
    goal_env["goal_file"].parent.mkdir(parents=True, exist_ok=True)
    state = {
        "goal": "goal already over budget",
        "remaining": -5,
        "max_turns": 5,
        "created": "2026-01-01T00:00:00+00:00",
    }
    goal_env["goal_file"].write_text(json.dumps(state), encoding="utf-8")

    exit_code = goal_gate.main(["extend", "5"])
    assert exit_code == 0

    new_state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert new_state["remaining"] == 0  # -5 + 5
    assert new_state["max_turns"] == 10  # 5 + 5
    assert new_state["extensions"] == 1


def test_extend_moves_the_exhaustion_boundary(goal_env, monkeypatch, capsys):
    """Regression: extending an in-flight goal must move WHERE stop-gate's
    budget runs out, not just cosmetically bump remaining/max_turns while
    the gate still exhausts at the original (pre-extend) boundary. Also
    pins that the history entry's turns_used reflects the extended
    max_turns, not the original one."""
    goal_gate.main(["set", "boundary check goal", "--max-turns", "2"])

    # One stop-gate call: remaining 2 -> 1. Without any extend, the very
    # next call would exhaust the original max_turns=2 budget.
    exit_code, out = run_stop_gate(monkeypatch, capsys)
    assert exit_code == 0
    assert "Turns left: 1" in json.loads(out)["reason"]

    extend_code = goal_gate.main(["extend", "5"])
    assert extend_code == 0
    state = json.loads(goal_env["goal_file"].read_text(encoding="utf-8"))
    assert state["remaining"] == 6  # 1 + 5
    assert state["max_turns"] == 7  # 2 + 5

    # It must now take 6 more stop-gate calls (not 1) to exhaust the budget.
    calls_made = 0
    exhausted_payload = None
    for _ in range(10):
        calls_made += 1
        exit_code, out = run_stop_gate(monkeypatch, capsys)
        assert exit_code == 0
        payload = json.loads(out)
        if "systemMessage" in payload:
            exhausted_payload = payload
            break
        assert payload["decision"] == "block"

    assert exhausted_payload is not None, "gate never exhausted within 10 calls"
    assert calls_made == 6
    assert not goal_env["goal_file"].exists()

    history = _read_history(goal_env["history_file"])
    assert history[-1]["outcome"] == "budget_exhausted"
    # turns_used = extended max_turns(7) - remaining(0) = 7, proving the
    # history reflects the EXTENDED budget, not the original max_turns=2.
    assert history[-1]["turns_used"] == 7


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


def _run_subprocess(
    python_exe: str, args: list[str], env: dict, stdin_text: str = ""
) -> subprocess.CompletedProcess:
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
    result_gate1 = _run_subprocess(
        system_python, ["stop-gate"], env, stdin_text=json.dumps({"session_id": "s1"})
    )
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


def test_end_to_end_subprocess_extend_error_paths_no_stdout(tmp_path):
    """extend's error paths (no active goal, invalid amount) must exit 1
    and never write anything to stdout — only stderr — when goal_gate.py is
    invoked as a real subprocess under sys.executable (the same interpreter
    running this test suite), and must never mutate an existing valid
    state file along the way."""
    goal_file = tmp_path / "e2e_extend_goal.json"
    history_file = tmp_path / "e2e_extend_history.jsonl"
    env = {
        **__import__("os").environ,
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_GOAL_HISTORY": str(history_file),
    }

    # extend with no active goal at all
    result_no_goal = _run_subprocess(sys.executable, ["extend", "5"], env)
    assert result_no_goal.returncode == 1
    assert result_no_goal.stdout == ""
    assert not goal_file.exists()

    # set a real goal, then hit each invalid-amount error path
    result_set = _run_subprocess(
        sys.executable, ["set", "e2e extend goal", "--max-turns", "3"], env
    )
    assert result_set.returncode == 0, f"stderr={result_set.stderr!r}"

    for bad_args in (["extend", "0"], ["extend", "101"], ["extend", "abc"], ["extend"]):
        result = _run_subprocess(sys.executable, bad_args, env)
        assert result.returncode == 1, f"args={bad_args} stderr={result.stderr!r}"
        assert result.stdout == "", f"args={bad_args} must not write to stdout on error"

    # none of the rejected calls above may have mutated the on-disk state
    state = json.loads(goal_file.read_text(encoding="utf-8"))
    assert state["remaining"] == 3
    assert state["max_turns"] == 3
    assert "extensions" not in state


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_end_to_end_subprocess_help_flag_exit_0(tmp_path, flag):
    goal_file = tmp_path / "e2e_help_goal.json"
    history_file = tmp_path / "e2e_help_history.jsonl"
    env = {
        **__import__("os").environ,
        "NAGI_GOAL_FILE": str(goal_file),
        "NAGI_GOAL_HISTORY": str(history_file),
    }
    result = _run_subprocess(sys.executable, [flag], env)
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert "usage: goal_gate.py" in result.stdout
    assert "extend" in result.stdout
