#!/usr/bin/env python3
"""CHECK palace-pg-timeouts: how many palace queries did postgres cancel at the statement timeout in the
last hour, and which kinds?

The database role's ``statement_timeout`` (30 s) cancels a query that runs too long, and the caller sees
an error or a stale answer. Nothing counted them: on 2026-11-18 JP asked "why did the palace time out?"
and the answer was 1,289 cancellations in 24 h (810 the status per-wing count, before a VACUUM; then the
room-graph aggregate, 100 s against 30 s). This check reads the database container's log for the last
``WINDOW_MIN`` minutes and counts ``canceling statement due to statement timeout``, by kind.

Kinds are fixed codes matched on the statement that follows each cancellation (``status-wing-count``,
``graph-room-aggregate``, ``metadata-lookup`` …, else ``other``). No statement text, parameter or value
ever reaches the line: a statement can carry drawer text.

- **fail**: the log could not be read, or at least ``FAIL_PER_H`` cancellations.
- **warn**: more than ``OK_PER_H`` cancellations.
- **ok**: at most ``OK_PER_H``.
A log window with no line at all is blind (postgres logs a checkpoint every few minutes): warn.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, line, run

NAME = "palace-pg-timeouts"
WINDOW_MIN = 60
OK_PER_H = int(os.environ.get("PG_TIMEOUTS_OK_PER_H", "10"))
FAIL_PER_H = int(os.environ.get("PG_TIMEOUTS_FAIL_PER_H", "60"))
CONTAINER = os.environ.get("PG_TIMEOUTS_CONTAINER", "mempalace-db")
_CANCEL = "canceling statement due to statement timeout"
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
# first match wins; patterns see the statement joined onto one line
KINDS = (
    ("status-wing-count", re.compile(r"SELECT wing, count\(\*\) FROM mempalace_drawers GROUP BY wing", re.IGNORECASE)),
    ("status-room-count", re.compile(r"SELECT room, count\(\*\) FROM mempalace_drawers WHERE room", re.IGNORECASE)),
    ("graph-room-aggregate", re.compile(r"array_agg\(DISTINCT wing", re.IGNORECASE)),
    ("kg-count", re.compile(r'count\(\*\) FROM [\w"]+\."(RELATION|Entity|MENTIONS)"', re.IGNORECASE)),
    ("kg-cypher", re.compile(r"\bcypher\(", re.IGNORECASE)),
    ("drawer-insert", re.compile(r"^\s*INSERT INTO", re.IGNORECASE)),
    ("keyword-search", re.compile(r"ts_rank_cd|plainto_tsquery", re.IGNORECASE)),
    ("vector-search", re.compile(r"<=>", re.IGNORECASE)),
    ("metadata-lookup", re.compile(r"metadata->>", re.IGNORECASE)),
    ("wing-page", re.compile(r'WHERE \(*"?wing"? = \$\d', re.IGNORECASE)),
    ("drawer-count", re.compile(r"COUNT\(\*\) FROM \"?mempalace_drawers", re.IGNORECASE)),
    ("schema-change", re.compile(r"^\s*ALTER TABLE", re.IGNORECASE)),
)


def kind(statement: str) -> str:
    for code, rx in KINDS:
        if rx.search(statement):
            return code
    return "other"


def cancellations(log: list[str]) -> Counter:
    """Kinds of the statements cancelled in ``log``: each cancellation's following ``STATEMENT:`` line,
    plus its continuation lines (a multi-line statement), up to the next stamped line."""
    out: Counter = Counter()
    i = 0
    while i < len(log):
        if _CANCEL in log[i]:
            stmt, j = "", i + 1
            while j < len(log) and j <= i + 3 and "STATEMENT:" not in log[j]:
                j += 1
            if j < len(log) and "STATEMENT:" in log[j]:
                stmt = log[j].split("STATEMENT:", 1)[1]
                k = j + 1
                while k < len(log) and not _STAMP.match(log[k]) and k < j + 40:
                    stmt += " " + log[k].strip()
                    k += 1
            out[kind(stmt) if stmt.strip() else "unknown"] += 1
        i += 1
    return out


def assess(log: list[str] | None) -> tuple[str, str]:
    if log is None:
        return "fail", f"the {CONTAINER} log could not be read"
    if not log:
        return "warn", f"the {CONTAINER} log returned 0 lines in {WINDOW_MIN} min (blind?)"
    c = cancellations(log)
    n = sum(c.values())
    top = ", ".join(f"{k}×{v}" for k, v in c.most_common(5)) or "none"
    fact = f"{n} statement timeout(s) in the last {WINDOW_MIN} min ({top})"
    if n >= FAIL_PER_H:
        return "fail", fact
    if n > OK_PER_H:
        return "warn", fact
    return "ok", fact


LOG_CMD = ["docker", "logs", "--since", f"{WINDOW_MIN}m", CONTAINER]


def _log() -> list[str] | None:
    try:
        out = subprocess.run(LOG_CMD, capture_output=True, text=True, errors="replace", timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout + out.stderr).splitlines()  # postgres writes its log to stderr


def body() -> None:
    state, fact = assess(_log())
    emit(NAME, line(NAME, state, fact))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
