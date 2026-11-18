#!/usr/bin/env python3
"""CHECK palace-pg-timeouts: how many palace queries did postgres cancel at the statement timeout in the
last hour, and which kinds?

The database role's ``statement_timeout`` (30 s) cancels a query that runs too long, and the caller sees
an error or a stale answer. Nothing counted them: on 2026-11-18 JP asked "why did the palace time out?"
and the answer was 1,289 cancellations in 24 h (810 the status per-wing count, before a VACUUM; then the
room-graph aggregate, 100 s against 30 s). This check reads the database container's log for the last
``WINDOW_MIN`` minutes and counts the ``ERROR:  canceling statement due to statement timeout`` records,
by kind. It is a sampled rolling hour (the timer runs hourly without a random delay), a rate, not an
exact ledger: a record that falls between two reads by a late start is not counted.

Parsing works on log RECORDS, not lines: a record starts with the ``log_line_prefix`` stamp
(``YYYY-MM-DD HH:MM:SS.mmm TZ [pid] LEVEL:``) and continues on unstamped lines. Only an ERROR record whose
message is the cancellation counts (the phrase inside a statement, a DETAIL or drawer text does not). Its
kind comes from the next STATEMENT record of the SAME backend pid. Kinds are fixed codes matched on that
statement with whitespace collapsed and quoted literals removed (``status-wing-count``,
``graph-room-aggregate``, ``metadata-lookup`` …, else ``other``; ``unknown`` when no statement was logged).
No statement text, parameter or value ever reaches the line: a statement can carry drawer text.

- **fail**: the log could not be read, the thresholds are invalid, or at least ``FAIL_PER_H`` cancellations.
- **warn**: more than ``OK_PER_H`` cancellations, or a window with no record at all (postgres logs a
  checkpoint every few minutes, so an empty window is blind).
- **ok**: at most ``OK_PER_H``.
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
CONTAINER = os.environ.get("PG_TIMEOUTS_CONTAINER", "mempalace-db")
_CANCEL = "canceling statement due to statement timeout"
# a record header: "2026-11-18 22:46:40.123 UTC [77] ERROR:  message"
_HEAD = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)? \S+ \[(\d+)\] ([A-Z]+):\s*(.*)$")
_STAMP = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
# literals, masked before classifying: dollar-quoted ($$…$$, $tag$…$tag$; not $1), escape strings
# (E'…' with backslash escapes), then standard strings ('…' with '' doubling)
_LITERAL = re.compile(
    r"\$([A-Za-z_][A-Za-z_0-9]*)?\$.*?\$\1\$"
    r"|(?<![A-Za-z0-9_])[Ee]'(?:[^'\\]|\\.|'')*'"
    r"|'(?:[^']|'')*'",
    re.DOTALL,
)
_SPACE = re.compile(r"\s+")
# first match wins; patterns see one statement, whitespace collapsed, quoted literals removed
KINDS = (
    ("status-wing-count", re.compile(r"SELECT wing, count\(\*\) FROM mempalace_drawers GROUP BY wing", re.IGNORECASE)),
    ("status-room-count", re.compile(r"SELECT room, count\(\*\) FROM mempalace_drawers WHERE room", re.IGNORECASE)),
    ("drawer-insert", re.compile(r"^INSERT INTO", re.IGNORECASE)),
    ("graph-room-aggregate", re.compile(r"array_agg\(DISTINCT wing", re.IGNORECASE)),
    ("kg-count", re.compile(r'count\(\*\) FROM [\w"]+\."(RELATION|Entity|MENTIONS)"', re.IGNORECASE)),
    ("kg-cypher", re.compile(r"\bcypher\(", re.IGNORECASE)),
    ("keyword-search", re.compile(r"ts_rank_cd|plainto_tsquery", re.IGNORECASE)),
    ("vector-search", re.compile(r"<=>", re.IGNORECASE)),
    ("metadata-lookup", re.compile(r"metadata->>", re.IGNORECASE)),
    ("wing-page", re.compile(r'WHERE \(*"?wing"? = \$\d', re.IGNORECASE)),
    ("drawer-count", re.compile(r"COUNT\(\*\) FROM \"?mempalace_drawers", re.IGNORECASE)),
    ("schema-change", re.compile(r"^ALTER TABLE", re.IGNORECASE)),
)


def kind(statement: str) -> str:
    s = _SPACE.sub(" ", _LITERAL.sub("?", statement)).strip()
    s = re.sub(r"\( ", "(", s)  # "count( *)" wrapped across lines
    for code, rx in KINDS:
        if rx.search(s):
            return code
    return "other"


def records(log: list[str]) -> list[tuple[str, str, str]]:
    """``(pid, level, message)`` per record; unstamped lines continue the record before them."""
    out: list[list[str]] = []
    for row in log:
        m = _HEAD.match(row)
        if m:
            out.append([m.group(1), m.group(2), m.group(3)])
        elif out and not _STAMP.match(row):
            out[-1][2] += " " + row.strip()
    return [(p, lvl, msg) for p, lvl, msg in out]


_SAME_ERROR = ("DETAIL", "HINT", "CONTEXT", "QUERY", "LOCATION")  # parts of one error report


def cancellations(log: list[str]) -> Counter:
    """Kinds of the statements cancelled in ``log``, each from its own backend's STATEMENT record. One pass,
    no window: a cancellation stays pending per backend pid until that backend's STATEMENT (its kind), or
    any other record of that backend that is not part of the same error report (``unknown``)."""
    out: Counter = Counter()
    pending: dict[str, int] = {}
    for pid, lvl, msg in records(log):
        if pid in pending:
            if lvl == "STATEMENT":
                out[kind(msg)] += 1
                pending[pid] -= 1
                if not pending[pid]:
                    del pending[pid]
                continue
            if lvl not in _SAME_ERROR:
                out["unknown"] += pending.pop(pid)
        if lvl == "ERROR" and msg.strip() == _CANCEL:
            pending[pid] = pending.get(pid, 0) + 1
    out["unknown"] += sum(pending.values())
    if not out["unknown"]:
        del out["unknown"]
    return out


def thresholds() -> tuple[int, int]:
    ok = int(os.environ.get("PG_TIMEOUTS_OK_PER_H", "10"))
    fail = int(os.environ.get("PG_TIMEOUTS_FAIL_PER_H", "60"))
    if not 0 <= ok < fail:
        raise ValueError("need 0 <= PG_TIMEOUTS_OK_PER_H < PG_TIMEOUTS_FAIL_PER_H")
    return ok, fail


def assess(log: list[str] | None, ok_per_h: int = 10, fail_per_h: int = 60) -> tuple[str, str]:
    if log is None:
        return "fail", "the database container log could not be read"
    if not records(log):
        return "warn", f"the database container log has 0 records in {WINDOW_MIN} min (blind?)"
    c = cancellations(log)
    n = sum(c.values())
    top = ", ".join(f"{k}×{v}" for k, v in c.most_common(5)) or "none"
    fact = f"{n} statement timeout(s) in the last {WINDOW_MIN} min ({top})"
    if n >= fail_per_h:
        return "fail", fact
    if n > ok_per_h:
        return "warn", fact
    return "ok", fact


LOG_CMD = ["docker", "logs", "--since", f"{WINDOW_MIN}m", CONTAINER]


def _log() -> list[str] | None:
    try:
        # postgres logs to stderr; merge the streams in arrival order
        out = subprocess.run(LOG_CMD, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             errors="replace", timeout=120, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def body() -> None:
    try:
        ok_per_h, fail_per_h = thresholds()
    except ValueError:
        emit(NAME, line(NAME, "fail", "invalid thresholds (PG_TIMEOUTS_OK_PER_H / _FAIL_PER_H)"))
        return
    state, fact = assess(_log(), ok_per_h, fail_per_h)
    emit(NAME, line(NAME, state, fact))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
