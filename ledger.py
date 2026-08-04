"""
Pure logic layer for nagi-ledger: an autonomous-development audit ledger.

This module contains no MCP-specific code. All functions operate on a
sqlite3.Connection and are directly unit-testable. server.py wraps these
functions as MCP tools.

Schema:
    actions     — a single autonomous action taken by an agent (tiered).
    dispatches  — a subagent dispatch, optionally later annotated with a verdict.
    approaches  — a recorded outcome (dead end / no-go / works) for a task,
                  so future attempts can check before retrying.
"""

from __future__ import annotations

import os
import re
import sqlite3
from difflib import SequenceMatcher
from pathlib import Path

VALID_TIERS = (0, 1, 2)
VALID_VERDICTS = ("CONFIRMED", "REFUTED", "PARTIAL")
VALID_OUTCOMES = ("DEAD_END", "NO_GO", "WORKS")

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  tier INTEGER NOT NULL CHECK(tier IN (0,1,2)),
  category TEXT NOT NULL,
  description TEXT NOT NULL,
  project TEXT
);
CREATE TABLE IF NOT EXISTS dispatches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  model TEXT NOT NULL,
  brief_summary TEXT NOT NULL,
  verdict TEXT CHECK(verdict IN ('CONFIRMED','REFUTED','PARTIAL') OR verdict IS NULL),
  verdict_ts TEXT,
  verdict_notes TEXT
);
CREATE TABLE IF NOT EXISTS approaches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL DEFAULT (datetime('now')),
  task TEXT NOT NULL,
  approach TEXT NOT NULL,
  outcome TEXT NOT NULL CHECK(outcome IN ('DEAD_END','NO_GO','WORKS')),
  reason TEXT NOT NULL
);
"""


def get_db_path() -> Path:
    """Resolve the ledger DB path.

    Single source of truth for the DB location: reads the NAGI_LEDGER_DB
    environment variable if set (used by tests / callers who want an
    isolated DB), otherwise defaults to ~/.nagi/ledger.db.
    """
    override = os.environ.get("NAGI_LEDGER_DB")
    if override:
        return Path(override)
    return Path.home() / ".nagi" / "ledger.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Open a sqlite3 connection to the ledger DB, creating the parent dir
    and schema if needed. Caller is responsible for closing the connection.
    """
    path = db_path if db_path is not None else get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # Async hooks can fire concurrently; WAL + busy_timeout prevent
    # "database is locked" from silently dropping a write.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables if they do not already exist."""
    conn.executescript(SCHEMA)
    conn.commit()


def _require_nonempty(value: str, field_name: str) -> str:
    if value is None or not str(value).strip():
        raise ValueError(f"'{field_name}' must be a non-empty string.")
    return str(value).strip()


def log_action(
    conn: sqlite3.Connection,
    tier: int,
    category: str,
    description: str,
    project: str | None = None,
) -> int:
    """Insert an autonomous action record. Returns the new row id.

    Raises ValueError with an actionable message if tier is not in {0,1,2}
    or if category/description are empty.
    """
    if tier not in VALID_TIERS:
        raise ValueError(
            f"Invalid tier {tier!r}: must be one of {VALID_TIERS} "
            "(0=info, 1=notable, 2=high-impact)."
        )
    category = _require_nonempty(category, "category")
    description = _require_nonempty(description, "description")
    project = str(project).strip() if project else None

    cur = conn.execute(
        "INSERT INTO actions (tier, category, description, project) VALUES (?, ?, ?, ?)",
        (tier, category, description, project),
    )
    conn.commit()
    return int(cur.lastrowid)


def log_dispatch(
    conn: sqlite3.Connection,
    task: str,
    agent_type: str,
    model: str,
    brief_summary: str,
) -> tuple[int, int]:
    """Insert a subagent dispatch record.

    Returns (id, retry_count) where retry_count is the number of PRIOR
    dispatches recorded for the same `task` string (0 for the first
    dispatch of a given task).

    Raises ValueError if any required field is empty.
    """
    task = _require_nonempty(task, "task")
    agent_type = _require_nonempty(agent_type, "agent_type")
    model = _require_nonempty(model, "model")
    brief_summary = _require_nonempty(brief_summary, "brief_summary")

    prior_count = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE task = ?", (task,)
    ).fetchone()["n"]

    cur = conn.execute(
        "INSERT INTO dispatches (task, agent_type, model, brief_summary) VALUES (?, ?, ?, ?)",
        (task, agent_type, model, brief_summary),
    )
    conn.commit()
    return int(cur.lastrowid), int(prior_count)


def log_verdict(
    conn: sqlite3.Connection,
    dispatch_id: int,
    verdict: str,
    notes: str | None = None,
) -> str:
    """Attach a verdict to an existing dispatch. Returns the dispatch's task string.

    Raises ValueError (with an actionable message) if:
      - dispatch_id does not exist
      - verdict is not one of CONFIRMED/REFUTED/PARTIAL
    """
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"Invalid verdict {verdict!r}: must be one of {VALID_VERDICTS}.")

    row = conn.execute("SELECT task FROM dispatches WHERE id = ?", (dispatch_id,)).fetchone()
    if row is None:
        raise ValueError(
            f"No dispatch found with id={dispatch_id}. "
            "Call ledger_log_dispatch first, or check the id via ledger_task_status."
        )

    conn.execute(
        "UPDATE dispatches SET verdict = ?, verdict_ts = datetime('now'), verdict_notes = ? "
        "WHERE id = ?",
        (verdict, notes, dispatch_id),
    )
    conn.commit()
    return row["task"]


def task_status(conn: sqlite3.Connection, task: str) -> dict:
    """Return retry/verdict status for a given task string.

    {
      "dispatch_count": int,
      "retry_count": int,          # dispatch_count - 1, floored at 0
      "last_verdict": str | None,  # verdict of the most recent dispatch, or None
      "over_retry_limit": bool     # True when retry_count >= 2
    }
    """
    task = _require_nonempty(task, "task")

    rows = conn.execute(
        "SELECT verdict FROM dispatches WHERE task = ? ORDER BY id DESC", (task,)
    ).fetchall()
    dispatch_count = len(rows)
    retry_count = max(dispatch_count - 1, 0)
    last_verdict = rows[0]["verdict"] if rows else None

    return {
        "dispatch_count": dispatch_count,
        "retry_count": retry_count,
        "last_verdict": last_verdict,
        "over_retry_limit": retry_count >= 2,
    }


def _since_clause_param(hours: float) -> str:
    return f"-{hours} hours"


def session_report(conn: sqlite3.Connection, since_hours: int = 24) -> str:
    """Build a markdown session report of actions + dispatches in the last N hours."""
    if since_hours <= 0:
        raise ValueError("'since_hours' must be a positive number.")

    since_param = _since_clause_param(since_hours)

    actions = conn.execute(
        "SELECT tier, category, description, project FROM actions "
        "WHERE ts >= datetime('now', ?) ORDER BY category, id",
        (since_param,),
    ).fetchall()

    dispatches = conn.execute(
        "SELECT task, agent_type, model, verdict FROM dispatches "
        "WHERE ts >= datetime('now', ?) ORDER BY id",
        (since_param,),
    ).fetchall()

    if not actions and not dispatches:
        return f"No entries in the last {since_hours}h."

    lines: list[str] = [f"## 自律実行リスト (last {since_hours}h)", ""]

    if actions:
        by_category: dict[str, list[sqlite3.Row]] = {}
        for row in actions:
            by_category.setdefault(row["category"], []).append(row)

        for category in sorted(by_category.keys()):
            lines.append(f"### {category}")
            for row in by_category[category]:
                suffix = f" ({row['project']})" if row["project"] else ""
                lines.append(f"- [Tier {row['tier']}] {row['description']}{suffix}")
            lines.append("")
    else:
        lines.append("(no actions)")
        lines.append("")

    lines.append("## Dispatches")
    if dispatches:
        for row in dispatches:
            verdict = row["verdict"] if row["verdict"] else "PENDING"
            lines.append(f"- {row['task']} → {row['agent_type']}/{row['model']} → {verdict}")
    else:
        lines.append("(no dispatches)")

    return "\n".join(lines)


def stats(conn: sqlite3.Connection, days: int = 7) -> dict:
    """Aggregate stats over the last N days.

    {
      "actions_by_tier": {"0": int, "1": int, "2": int},
      "actions_by_category": {category: int, ...},
      "dispatch_count": int,
      "verdicts": {"CONFIRMED": int, "REFUTED": int, "PARTIAL": int, "PENDING": int}
    }
    """
    if days <= 0:
        raise ValueError("'days' must be a positive number.")

    since_param = f"-{days} days"

    actions_by_tier = {str(t): 0 for t in VALID_TIERS}
    for row in conn.execute(
        "SELECT tier, COUNT(*) AS n FROM actions WHERE ts >= datetime('now', ?) GROUP BY tier",
        (since_param,),
    ).fetchall():
        actions_by_tier[str(row["tier"])] = row["n"]

    actions_by_category: dict[str, int] = {}
    for row in conn.execute(
        "SELECT category, COUNT(*) AS n FROM actions WHERE ts >= datetime('now', ?) "
        "GROUP BY category",
        (since_param,),
    ).fetchall():
        actions_by_category[row["category"]] = row["n"]

    dispatch_count = conn.execute(
        "SELECT COUNT(*) AS n FROM dispatches WHERE ts >= datetime('now', ?)",
        (since_param,),
    ).fetchone()["n"]

    verdicts = {v: 0 for v in VALID_VERDICTS}
    verdicts["PENDING"] = 0
    for row in conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM dispatches WHERE ts >= datetime('now', ?) "
        "GROUP BY verdict",
        (since_param,),
    ).fetchall():
        key = row["verdict"] if row["verdict"] else "PENDING"
        verdicts[key] = row["n"]

    return {
        "actions_by_tier": actions_by_tier,
        "actions_by_category": actions_by_category,
        "dispatch_count": int(dispatch_count),
        "verdicts": verdicts,
    }


def log_approach(
    conn: sqlite3.Connection,
    task: str,
    approach: str,
    outcome: str,
    reason: str,
) -> int:
    """Record the outcome of an approach tried (or considered) for a task.
    Returns the new row id.

    Raises ValueError with an actionable message if task/approach/reason are
    empty, or if outcome is not one of DEAD_END/NO_GO/WORKS.
    """
    task = _require_nonempty(task, "task")
    approach = _require_nonempty(approach, "approach")
    reason = _require_nonempty(reason, "reason")
    if outcome not in VALID_OUTCOMES:
        raise ValueError(
            f"Invalid outcome {outcome!r}: must be one of {VALID_OUTCOMES} "
            "(DEAD_END=tried and failed, NO_GO=decided against without trying, "
            "WORKS=confirmed working)."
        )

    cur = conn.execute(
        "INSERT INTO approaches (task, approach, outcome, reason) VALUES (?, ?, ?, ?)",
        (task, approach, outcome, reason),
    )
    conn.commit()
    return int(cur.lastrowid)


def check_approaches(conn: sqlite3.Connection, task: str) -> dict:
    """Look up all recorded approaches for a task, bucketed by outcome.

    {
      "task": task,
      "dead_ends": [{"approach", "reason", "ts"}, ...],  # newest first
      "no_gos": [{"approach", "reason", "ts"}, ...],      # newest first
      "works": [{"approach", "reason", "ts"}, ...],       # newest first
      "total": int
    }
    """
    task = _require_nonempty(task, "task")

    rows = conn.execute(
        "SELECT approach, outcome, reason, ts FROM approaches WHERE task = ? ORDER BY id DESC",
        (task,),
    ).fetchall()

    dead_ends = []
    no_gos = []
    works = []
    for row in rows:
        item = {"approach": row["approach"], "reason": row["reason"], "ts": row["ts"]}
        if row["outcome"] == "DEAD_END":
            dead_ends.append(item)
        elif row["outcome"] == "NO_GO":
            no_gos.append(item)
        elif row["outcome"] == "WORKS":
            works.append(item)

    return {
        "task": task,
        "dead_ends": dead_ends,
        "no_gos": no_gos,
        "works": works,
        "total": len(rows),
    }


# --- search / similarity --------------------------------------------------
#
# These exist to catch the failure mode where two agents dispatch the SAME
# underlying task under two DIFFERENT task strings (e.g. "Build the YouTube
# knowledge intake PoC" vs "Build YouTube-to-Markdown intake PoC" fired 50s
# apart) — ledger_task_status/ledger_check_approaches require an exact
# string match, so a reworded duplicate sails through with retry_count=0.

_VALID_KINDS = ("actions", "dispatches", "approaches")

# Matches ASCII/digit "words". Japanese (and other CJK) text has no space
# boundaries, so this alone misses it entirely — see _tokenize().
_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> set[str]:
    """Tokenize text into a lowercase set of match units for scoring.

    Splits ASCII words on non-alphanumeric boundaries. Whatever text is
    NOT covered by an ASCII word (crucially: Japanese/CJK text, which has
    no word boundaries for [a-z0-9]+ to find) falls back to character
    2-grams. This is a genuine FALLBACK, not a blend applied to
    everything — 2-gramming pure-ASCII text too would make e.g. "content"
    and "context" share bigrams like "co"/"nt"/"te" and produce noisy
    false-positive matches between otherwise unrelated English strings.
    Restricting 2-grams to the leftover (non-ASCII-word) text avoids that
    while still making Japanese searchable: "大規模ログ" has no ASCII
    words, so it tokenizes entirely via 2-grams to {"大規","規模","模ロ",
    "ログ"}, which overlaps real ledger text like "大規模ログ2(5MB)の
    grep+サンプリング調査".
    """
    text = text.lower()
    tokens = set(_WORD_RE.findall(text))
    remainder = re.sub(r"\s+", "", _WORD_RE.sub(" ", text))
    tokens.update(remainder[i : i + 2] for i in range(len(remainder) - 1))
    return tokens


def _score(query_tokens: set[str], text: str) -> float:
    """Fraction of query tokens present in `text`'s tokens. 0.0 if either side is empty."""
    if not query_tokens or not text:
        return 0.0
    text_tokens = _tokenize(text)
    if not text_tokens:
        return 0.0
    return len(query_tokens & text_tokens) / len(query_tokens)


def _similarity(a: str, b: str) -> float:
    """Similarity of two task strings, combining two orthogonal stdlib signals.

    - difflib.SequenceMatcher.ratio(): character-level edit-similarity.
      Rewards shared substrings/order, but ALSO gives a misleadingly high
      score to two strings that merely share a prefix (e.g. "Build X" vs
      "Build Y" scores ~0.5+ on ratio() alone even when X and Y are
      unrelated projects).
    - token Jaccard, using the SAME _tokenize() as search() (so CJK text
      without spaces still tokenizes via the 2-gram fallback): rewards
      shared *content* regardless of position, but is 0 whenever the two
      strings share no vocabulary at all, even if they happen to be
      lexically close by accident.

    Taking the MIN of the two requires both signals to agree. This is what
    suppresses the "Build X" vs "Build Y" false positive: measured against
    real ledger data, "Build YouTube-to-Markdown intake PoC" vs "Build
    accounts domain and fake provider" scores ratio=0.56 but jaccard=0.11
    (only "build" overlaps) -> min=0.11. The real duplicate, "Build
    YouTube-to-Markdown intake PoC" vs "Build the YouTube knowledge intake
    PoC", scores ratio=0.73 and jaccard=0.43 -> min=0.43. That ~4x margin
    (0.11 vs 0.43) is why min_score defaults to 0.35.

    *** No threshold separates the two classes. Do not tune this. ***

    A full pairwise sweep of the production ledger (80 distinct dispatch
    task strings = 3160 pairs) crosses 0.35 on 20 pairs, and only ~3 of
    those are genuine reworded duplicates. Worse, several pairs that are
    plainly DIFFERENT tasks score ABOVE the duplicate this function exists
    to catch (0.444):

        0.600  "nagi-ledger MCP verification"     vs "... implementation"
        0.500  "Deep-read publishing/marketing …" vs "Deep-read context/handover …"
        0.455  "Analyze 2026-04-30 X post log"    vs "Analyze 2026-04-28 price research log"

    Raising min_score to kill those also kills the target; lowering it makes
    the eight "Deep-read <topic> cluster" pairs fire together. The cause is
    that tasks here are named with deliberate shared prefixes, so string
    distance is not a measure of "same work" at all -- the residual is not
    the kind of thing a threshold can enumerate.

    Consequence for callers: treat a hit as "go look", never as a verdict.
    It is surfaced as a warning on ledger_log_dispatch, not a block.
    """
    a_low, b_low = a.lower(), b.lower()
    ratio = SequenceMatcher(None, a_low, b_low).ratio()
    ta, tb = _tokenize(a), _tokenize(b)
    jaccard = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return min(ratio, jaccard)


def _action_index_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": "actions",
        "ts": row["ts"],
        "title": row["description"],
        "status": row["tier"],
    }


def _dispatch_index_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": "dispatches",
        "ts": row["ts"],
        "title": row["task"],
        "status": row["verdict"] if row["verdict"] else "PENDING",
    }


def _approach_index_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "kind": "approaches",
        "ts": row["ts"],
        "title": f"{row['task']}: {row['approach']}",
        "status": row["outcome"],
    }


def _actions_rows(conn: sqlite3.Connection, since_clause: str = "", params: tuple = ()):
    return conn.execute(
        f"SELECT id, ts, tier, category, description, project FROM actions WHERE 1=1{since_clause}",
        params,
    ).fetchall()


def _dispatches_rows(conn: sqlite3.Connection, since_clause: str = "", params: tuple = ()):
    return conn.execute(
        f"SELECT id, ts, task, brief_summary, verdict, verdict_notes FROM dispatches "
        f"WHERE 1=1{since_clause}",
        params,
    ).fetchall()


def _approaches_rows(conn: sqlite3.Connection, since_clause: str = "", params: tuple = ()):
    return conn.execute(
        f"SELECT id, ts, task, approach, outcome, reason FROM approaches WHERE 1=1{since_clause}",
        params,
    ).fetchall()


def search(
    conn: sqlite3.Connection,
    query: str,
    kinds: list[str] | None = None,
    limit: int = 20,
    offset: int = 0,
    since_days: float | None = None,
) -> list[dict]:
    """Keyword search across actions/dispatches/approaches. Returns INDEX ROWS ONLY.

    Never returns body text (actions.description IS the index title, but
    dispatches.brief_summary/verdict_notes and approaches.reason are used
    only for scoring and are never included in the result) — this is the
    whole point: a caller can page through many matches cheaply, then fetch
    full detail for just the id(s) it cares about via the existing
    ledger_task_status / ledger_check_approaches tools.

    Matching is word-level and case-insensitive with a character-2-gram
    fallback (see _tokenize) so it also works on Japanese text, which has
    no whitespace word boundaries.

    Args:
        query: search text. Must be non-empty.
        kinds: subset of ("actions","dispatches","approaches") to search.
            None (default) searches all three.
        limit: max rows to return. Must be positive.
        offset: rows to skip (for paging). Must be non-negative.
        since_days: if set, only rows from the last N days. Must be positive.

    Returns:
        list of {"id","kind","ts","title","status"}, sorted by relevance
        score (desc), then recency (desc) as a tiebreaker.

    Raises:
        ValueError if query is empty, kinds contains an unknown value,
        limit/offset/since_days are out of range.
    """
    query = _require_nonempty(query, "query")
    selected = set(kinds) if kinds else set(_VALID_KINDS)
    unknown = selected - set(_VALID_KINDS)
    if unknown:
        raise ValueError(f"Invalid kinds {sorted(unknown)}: must be a subset of {_VALID_KINDS}.")
    if limit <= 0:
        raise ValueError("'limit' must be a positive integer.")
    if offset < 0:
        raise ValueError("'offset' must be non-negative.")

    since_clause, since_params = "", ()
    if since_days is not None:
        if since_days <= 0:
            raise ValueError("'since_days' must be a positive number.")
        since_clause = " AND ts >= datetime('now', ?)"
        since_params = (f"-{since_days} days",)

    query_tokens = _tokenize(query)
    scored: list[tuple[float, dict]] = []

    if "actions" in selected:
        for row in _actions_rows(conn, since_clause, since_params):
            score = (
                _score(query_tokens, row["description"])
                + _score(query_tokens, row["category"])
                + _score(query_tokens, row["project"] or "")
            )
            if score > 0:
                scored.append((score, _action_index_row(row)))

    if "dispatches" in selected:
        for row in _dispatches_rows(conn, since_clause, since_params):
            score = (
                _score(query_tokens, row["task"])
                + _score(query_tokens, row["brief_summary"])
                + _score(query_tokens, row["verdict_notes"] or "")
            )
            if score > 0:
                scored.append((score, _dispatch_index_row(row)))

    if "approaches" in selected:
        for row in _approaches_rows(conn, since_clause, since_params):
            score = (
                _score(query_tokens, row["task"])
                + _score(query_tokens, row["approach"])
                + _score(query_tokens, row["reason"])
            )
            if score > 0:
                scored.append((score, _approach_index_row(row)))

    # Stable-sort twice: recency first (so ties break newest-first), then
    # score (stable sort preserves the recency order within equal scores).
    scored.sort(key=lambda pair: pair[1]["ts"], reverse=True)
    scored.sort(key=lambda pair: pair[0], reverse=True)

    return [row for _, row in scored[offset : offset + limit]]


def similar_tasks(
    conn: sqlite3.Connection,
    task: str,
    limit: int = 5,
    min_score: float = 0.35,
) -> list[dict]:
    """Find dispatches/approaches whose task string is textually similar to `task`.

    Use this to catch reworded duplicates that an exact-string match
    (ledger_task_status) would miss — e.g. dispatching "Build
    YouTube-to-Markdown intake PoC" moments after "Build the YouTube
    knowledge intake PoC" was already dispatched under a different string.

    Args:
        task: the task string to compare against. Must be non-empty.
        limit: max results. Must be positive.
        min_score: minimum similarity (see _similarity) to include.
            Defaults to 0.35 — see _similarity's docstring for how that
            threshold was picked from real ledger data.

    Returns:
        list of {"kind","id","task","score","ts","verdict"} sorted by
        score desc. "verdict" holds the dispatch verdict or the approach
        outcome (whichever applies to that row's kind). Empty list if
        nothing scores >= min_score.

    Raises:
        ValueError if task is empty or limit is not positive.
    """
    task = _require_nonempty(task, "task")
    if limit <= 0:
        raise ValueError("'limit' must be a positive integer.")

    candidates: list[tuple[str, int, str, str, str | None]] = []
    for row in conn.execute("SELECT id, ts, task, verdict FROM dispatches").fetchall():
        candidates.append(("dispatches", row["id"], row["task"], row["ts"], row["verdict"]))
    for row in conn.execute("SELECT id, ts, task, outcome FROM approaches").fetchall():
        candidates.append(("approaches", row["id"], row["task"], row["ts"], row["outcome"]))

    scored = []
    for kind, cid, ctask, ts, verdict in candidates:
        score = _similarity(task, ctask)
        if score >= min_score:
            scored.append(
                {
                    "kind": kind,
                    "id": cid,
                    "task": ctask,
                    "score": round(score, 4),
                    "ts": ts,
                    "verdict": verdict,
                }
            )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:limit]


def timeline(
    conn: sqlite3.Connection,
    anchor_kind: str,
    anchor_id: int,
    before: int = 3,
    after: int = 3,
) -> list[dict]:
    """Return the rows immediately before/after an anchor row, across all tables.

    Merges actions/dispatches/approaches into a single chronological
    sequence (ordered by ts, then kind, then id to break ties — sqlite
    `datetime('now')` timestamps only have second resolution, so ties are
    common when a burst of rows land in the same second), locates the
    anchor, and slices a window of `before` rows preceding it and `after`
    rows following it.

    Args:
        anchor_kind: one of "actions", "dispatches", "approaches".
        anchor_id: the id of the anchor row within that table.
        before: how many preceding rows to include. Must be non-negative.
        after: how many following rows to include. Must be non-negative.

    Returns:
        list of index rows (same shape as search()'s: id/kind/ts/title/status),
        plus "anchor": true on the row that matches (anchor_kind, anchor_id)
        and "anchor": false on every other row.

    Raises:
        ValueError if anchor_kind is invalid, before/after are negative, or
        no row with (anchor_kind, anchor_id) exists.
    """
    if anchor_kind not in _VALID_KINDS:
        raise ValueError(f"Invalid anchor_kind {anchor_kind!r}: must be one of {_VALID_KINDS}.")
    if before < 0 or after < 0:
        raise ValueError("'before' and 'after' must be non-negative.")

    all_rows = (
        [_action_index_row(r) for r in _actions_rows(conn)]
        + [_dispatch_index_row(r) for r in _dispatches_rows(conn)]
        + [_approach_index_row(r) for r in _approaches_rows(conn)]
    )
    all_rows.sort(key=lambda r: (r["ts"], r["kind"], r["id"]))

    anchor_pos = next(
        (i for i, r in enumerate(all_rows) if r["kind"] == anchor_kind and r["id"] == anchor_id),
        None,
    )
    if anchor_pos is None:
        raise ValueError(f"No {anchor_kind} row found with id={anchor_id}.")

    start = max(0, anchor_pos - before)
    end = min(len(all_rows), anchor_pos + after + 1)

    result = []
    for i in range(start, end):
        item = dict(all_rows[i])
        item["anchor"] = i == anchor_pos
        result.append(item)
    return result
