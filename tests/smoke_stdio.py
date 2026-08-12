"""Manual smoke test: spawn server.py over stdio, list tools, call two of them.

Not a pytest test (spawning a subprocess MCP server per test is slow / not
what pytest is for here) — run directly:

    .venv\\Scripts\\python.exe tests\\smoke_stdio.py

Exits non-zero and prints a clear failure reason if anything is wrong.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVER_PATH = PROJECT_ROOT / "server.py"

EXPECTED_TOOLS = {
    "ledger_log_action",
    "ledger_log_dispatch",
    "ledger_log_verdict",
    "ledger_task_status",
    "ledger_session_report",
    "ledger_stats",
    "ledger_log_approach",
    "ledger_check_approaches",
    "ledger_search",
    "ledger_similar_tasks",
}


async def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "smoke_ledger.db"
        env = dict(os.environ)
        env["NAGI_LEDGER_DB"] = str(db_path)

        server_params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER_PATH)],
            env=env,
        )

        async with (
            stdio_client(server_params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()

            tools_result = await session.list_tools()
            tool_names = {t.name for t in tools_result.tools}
            print(f"Tools found: {sorted(tool_names)}")
            missing = EXPECTED_TOOLS - tool_names
            if missing:
                print(f"FAIL: missing tools: {missing}")
                return 1
            print(f"PASS: all {len(EXPECTED_TOOLS)} expected tool names present")
            extra = tool_names - EXPECTED_TOOLS
            if extra:
                print(f"FAIL: server exposes tools this smoke test does not know: {extra}")
                return 1

            # ledger_log_action end-to-end
            result = await session.call_tool(
                "ledger_log_action",
                {
                    "tier": 1,
                    "category": "smoke_test",
                    "description": "stdio smoke test action",
                    "project": "nagi-ledger-mcp",
                },
            )
            print(f"ledger_log_action raw result: {result}")
            if result.isError:
                print(f"FAIL: ledger_log_action returned an error: {result}")
                return 1
            data = result.structuredContent
            if not data or "id" not in data:
                print(f"FAIL: ledger_log_action did not return an id: {data}")
                return 1
            print(f"PASS: ledger_log_action -> id={data['id']}")

            # ledger_log_dispatch + ledger_task_status end-to-end
            dispatch_result = await session.call_tool(
                "ledger_log_dispatch",
                {
                    "task": "smoke-task",
                    "agent_type": "fork",
                    "model": "sonnet",
                    "brief_summary": "smoke test dispatch",
                },
            )
            if dispatch_result.isError:
                print(f"FAIL: ledger_log_dispatch returned an error: {dispatch_result}")
                return 1
            dispatch_data = dispatch_result.structuredContent
            print(f"PASS: ledger_log_dispatch -> {dispatch_data}")
            if dispatch_data.get("retry_count") != 0:
                print(f"FAIL: expected retry_count 0 on first dispatch, got {dispatch_data}")
                return 1

            status_result = await session.call_tool("ledger_task_status", {"task": "smoke-task"})
            if status_result.isError:
                print(f"FAIL: ledger_task_status returned an error: {status_result}")
                return 1
            status_data = status_result.structuredContent
            print(f"PASS: ledger_task_status -> {status_data}")
            if status_data.get("dispatch_count") != 1:
                print(f"FAIL: expected dispatch_count 1, got {status_data}")
                return 1

            # Error path: invalid tier should surface as a tool error, not a silent pass.
            bad_result = await session.call_tool(
                "ledger_log_action",
                {"tier": 9, "category": "x", "description": "y"},
            )
            print(f"ledger_log_action (bad tier) raw result: {bad_result}")
            if not bad_result.isError:
                print("FAIL: expected an error result for invalid tier=9")
                return 1
            print("PASS: invalid tier correctly surfaced as tool error")

            # ledger_log_approach + ledger_check_approaches end-to-end
            approach_result = await session.call_tool(
                "ledger_log_approach",
                {
                    "task": "smoke-approach-task",
                    "approach": "tried rm -rf on the venv",
                    "outcome": "DEAD_END",
                    "reason": "broke unrelated deps",
                },
            )
            if approach_result.isError:
                print(f"FAIL: ledger_log_approach returned an error: {approach_result}")
                return 1
            approach_data = approach_result.structuredContent
            print(f"PASS: ledger_log_approach -> {approach_data}")
            if not approach_data or "id" not in approach_data:
                print(f"FAIL: ledger_log_approach did not return an id: {approach_data}")
                return 1

            check_result = await session.call_tool(
                "ledger_check_approaches", {"task": "smoke-approach-task"}
            )
            if check_result.isError:
                print(f"FAIL: ledger_check_approaches returned an error: {check_result}")
                return 1
            check_data = check_result.structuredContent
            print(f"PASS: ledger_check_approaches -> {check_data}")
            if check_data.get("total") != 1:
                print(f"FAIL: expected total 1, got {check_data}")
                return 1
            dead_ends = check_data.get("dead_ends", [])
            if len(dead_ends) != 1 or dead_ends[0]["approach"] != "tried rm -rf on the venv":
                print(f"FAIL: expected recorded approach in dead_ends, got {check_data}")
                return 1
            if check_data.get("no_gos") or check_data.get("works"):
                print(f"FAIL: expected no_gos/works empty, got {check_data}")
                return 1
            print("PASS: ledger_log_approach -> ledger_check_approaches round-trip correct")

            # ledger_search end-to-end: the dispatch logged above must be findable.
            search_result = await session.call_tool("ledger_search", {"query": "smoke"})
            if search_result.isError:
                print(f"FAIL: ledger_search returned an error: {search_result}")
                return 1
            search_data = search_result.structuredContent
            rows = search_data.get("result") if isinstance(search_data, dict) else search_data
            if not rows or not any(r.get("title") == "smoke-task" for r in rows):
                print(f"FAIL: ledger_search('smoke') did not surface smoke-task: {rows}")
                return 1
            print(f"PASS: ledger_search -> {len(rows)} row(s), smoke-task present")

            # ledger_similar_tasks end-to-end: a near-duplicate wording must match.
            similar_result = await session.call_tool("ledger_similar_tasks", {"task": "smoke task"})
            if similar_result.isError:
                print(f"FAIL: ledger_similar_tasks returned an error: {similar_result}")
                return 1
            similar_data = similar_result.structuredContent
            sim_rows = (
                similar_data.get("result") if isinstance(similar_data, dict) else similar_data
            )
            if not sim_rows or not any(r.get("task") == "smoke-task" for r in sim_rows):
                print(
                    "FAIL: ledger_similar_tasks('smoke task') did not surface"
                    f" smoke-task: {sim_rows}"
                )
                return 1
            print("PASS: ledger_similar_tasks -> near-duplicate smoke-task found")

    print("\nSMOKE TEST: ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
