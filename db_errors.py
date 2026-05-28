"""DB-error observability ring buffer (#97/#99/#108/#110) — extracted from
main.py per #101 refactor (third slice).

Today's morning OOM cluster (2026-05-28; postgres killed twice inside its
docker memcg at 08:57 + 09:19) surfaced 26+ `OperationalError: connection
is closed` events that were invisible to /health — the daemon process
stayed up the whole time, but in-flight queries returned errors. Same
silent-failure-under-healthy-surface shape #92 was filed to close, just
for the postgres dependency.

This module owns the bounded ring buffer + classifier + summary helper
that hoist that signal into /health.db_errors. Producers (`_connect_postgres`
and the various daemon-side postgres-touching paths) call ``record(exc)``;
the /health handler calls ``summary()``.

main.py re-exports these under their original ``_``-prefixed names so
existing call sites + test patches that target ``main._record_db_error``
etc. keep working unchanged.
"""
from __future__ import annotations

import collections
import threading

# Bounded ring buffer of recent (timestamp, pattern, preview) tuples. 1000
# entries × ~100 bytes = ~100 KB ceiling; the 5-minute window we summarize
# over is usually <100 entries even during a postgres flap.
DB_ERROR_LOG: "collections.deque[tuple[float, str, str]]" = collections.deque(maxlen=1000)

# Lock guarding both append and iteration. deque.append is thread-safe in
# CPython, but iterating (e.g. ``list(deque)``) under concurrent mutation
# can produce duplicated or skipped entries; the lock gives us a clean
# snapshot for /health responses. Cost is negligible — observability code
# runs at most a few hundred times per /health probe.
DB_ERROR_LOG_LOCK = threading.Lock()


def classify_db_error(exc: BaseException) -> str:
    """Bucket a psycopg2/psycopg OperationalError by its surface message.

    Categories match what the 2026-05-28 journal grep cataloged plus a
    catch-all so the bucket vocabulary stays bounded:

      * ``in_recovery``       — server in recovery mode (post-OOM restart)
      * ``connection_closed`` — the connection is closed (post-mortem)
      * ``server_closed``     — server closed connection unexpectedly
      * ``connection_lost``   — connection lost mid-query
      * ``connect_failed``    — initial connect could not reach server
      * ``timeout``           — statement_timeout fired
      * ``other``             — anything not above
    """
    msg = str(exc).lower()
    if "in recovery mode" in msg:
        return "in_recovery"
    if "consuming input failed" in msg or "server closed the connection" in msg:
        return "server_closed"
    if "connection is lost" in msg or "connection lost" in msg:
        return "connection_lost"
    if "connection is closed" in msg:
        return "connection_closed"
    if (
        "connection failed" in msg
        or "could not connect" in msg
        or "connection refused" in msg
    ):
        return "connect_failed"
    if "statement timeout" in msg or "canceling statement" in msg:
        return "timeout"
    return "other"


def record_db_error(exc: BaseException) -> None:
    """Append a classified DB error to the ring buffer with a preview.

    Lock-guarded against concurrent ``db_errors_summary`` iteration so
    snapshots stay consistent under load. Preview is truncated to 200
    chars so a runaway error message can't bloat the ring buffer beyond
    its rough memory ceiling.
    """
    import time
    pattern = classify_db_error(exc)
    preview = str(exc)[:200]
    with DB_ERROR_LOG_LOCK:
        DB_ERROR_LOG.append((time.time(), pattern, preview))


def db_errors_summary(window_s: float = 300.0) -> dict:
    """Aggregate the ring buffer over the last ``window_s`` seconds.

    Returns counts overall and per-pattern, plus the timestamp of the
    newest error. Lock-guarded snapshot so concurrent ``record_db_error``
    appends from executor threads can't yield skipped/duplicated entries.
    """
    import datetime as _dt
    import time
    cutoff = time.time() - window_s
    by_pattern: dict[str, int] = {}
    total = 0
    newest_ts = 0.0
    # Acquire the lock just long enough to snapshot — iteration over the
    # snapshot then happens lock-free, so /health probing won't block
    # concurrent error recording.
    with DB_ERROR_LOG_LOCK:
        snapshot = list(DB_ERROR_LOG)
    for ts, pattern, _preview in snapshot:
        if ts < cutoff:
            continue
        total += 1
        by_pattern[pattern] = by_pattern.get(pattern, 0) + 1
        if ts > newest_ts:
            newest_ts = ts
    return {
        "total_last_window": total,
        "window_seconds": int(window_s),
        "by_pattern": by_pattern,
        # tz-aware UTC iso — naive local-time strings confuse consumers
        # running in other timezones (especially monitoring tools that
        # diff against UTC `now`).
        "newest_ts": (
            _dt.datetime.fromtimestamp(newest_ts, tz=_dt.timezone.utc)
                .isoformat(timespec="seconds")
            if newest_ts > 0 else None
        ),
    }
