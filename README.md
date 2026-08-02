# nagi-ledger

An autonomous-development audit ledger, exposed as a local MCP (Model
Context Protocol) stdio server.

It records, in SQLite:

- **actions** 窶・autonomous actions an agent takes on its own initiative,
  tagged with an impact tier (0/1/2) and category.
- **dispatches** 窶・subagent dispatches (task, agent type, model), each
  optionally annotated with a verification **verdict**
  (`CONFIRMED` / `REFUTED` / `PARTIAL`).

It exposes 6 MCP tools so an agent can introspect its own autonomous-dev
loop 窶・e.g. check whether it has already retried the same task too many
times before dispatching again 窶・and produce human-readable session
reports.

## Storage

Default DB path: `%USERPROFILE%\.nagi\ledger.db` (parent directory is
created automatically on first use).

Override with the `NAGI_LEDGER_DB` environment variable 窶・this is how
tests point the server at an isolated temp file instead of the real
ledger.

## Tools

| Tool | Purpose |
|---|---|
| `ledger_log_action(tier, category, description, project=None)` | Record an autonomous action. |
| `ledger_log_dispatch(task, agent_type, model, brief_summary)` | Record a subagent dispatch; returns the prior-retry count for that task. |
| `ledger_log_verdict(dispatch_id, verdict, notes=None)` | Attach a verification verdict to a dispatch. |
| `ledger_task_status(task)` | Retry/verdict status for a task, including `over_retry_limit` (true at retry_count >= 2). |
| `ledger_session_report(since_hours=24)` | Markdown report of recent actions (grouped by category) and dispatches. |
| `ledger_stats(days=7)` | Aggregate counts: actions by tier/category, dispatch count, verdict breakdown. |

## Install

```powershell
cd C:\Dev\nagi-ledger-mcp
python -m venv .venv
.venv\Scripts\pip.exe install -r requirements.txt
```

## Run tests

```powershell
.venv\Scripts\python.exe -m pytest tests -v
```

## Register with Claude Code

This project does not register itself. To register it in a Claude Code
session, run (from any directory):

```powershell
claude mcp add nagi-ledger -s user -- C:\Dev\nagi-ledger-mcp\.venv\Scripts\python.exe C:\Dev\nagi-ledger-mcp\server.py
```

## Files

- `ledger.py` 窶・pure SQLite logic (DB init, insert/query functions), fully
  testable without MCP involved.
- `server.py` 窶・FastMCP server; thin wrappers around `ledger.py` exposed
  as the 6 tools above. Runs over stdio when executed directly
  (`python server.py`).
- `tests/test_ledger.py` 窶・pytest suite covering schema creation,
  validation, retry counting, verdict attachment, task status, session
  reports, and stats.

