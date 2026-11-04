"""Instruments that make silent ingest failures visible.

Measured 2026-11-04 on familiar: for eight days every hook save from a
session outside ~ was rejected with 401 (16.5K rejections in hook.log), and a
601 MB transcript was "mined" every 30 minutes while the miner skipped it for
being over its 500 MB cap. Nothing on any status surface said so: a rejected
POST read as healthy, and a skipped mine exited 0 and was recorded as mined.

- ``AuthRejectionCounter`` counts every 401 the daemon sends, whatever route
  raised it; ``auth_rejection_summary()`` reports the last 24 h for
  ``/status/fast``.
- ``source_status(path)`` reports, for one transcript, what the palace holds
  from it against the file itself (``/status/source``).
- ``transcript_over_cap(path)`` is the miner's own size rule, so the drain can
  log a skip instead of spawning a mine that silently does nothing.
"""

from __future__ import annotations

import collections
import os
import stat
import threading
import time

_WINDOW_S = 24 * 3600
_PROCESS_START = time.time()

# (timestamp, path) of each 401, newest last. Bounded so a flood of rejected
# requests costs a fixed amount of memory; the summary says when it saturated.
_REJECTIONS: "collections.deque[tuple[float, str]]" = collections.deque(maxlen=50_000)
_LOCK = threading.Lock()


class AuthRejectionCounter:
    """Pure ASGI middleware: record the path of every response sent with 401.

    Pure ASGI rather than ``@app.middleware("http")`` so streaming responses
    are passed through untouched; it only peeks at ``http.response.start``.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start" and message.get("status") == 401:
                record_rejection(scope.get("path", ""))
            await send(message)

        await self.app(scope, receive, _send)


_PATH_MAX = 128


def record_rejection(path: str, now: float | None = None) -> None:
    # Unauthenticated callers choose the path, so store a bounded prefix: at
    # maxlen entries the deque then costs a few MB, never URL-length x 50K.
    with _LOCK:
        _REJECTIONS.append((time.time() if now is None else now, str(path)[:_PATH_MAX]))


def auth_rejection_summary(now: float | None = None) -> dict:
    """401s in the last 24 h. ``since`` is the daemon start when it is younger.

    The count lives in memory, so a restart zeroes it; ``window_complete`` is
    false until the daemon has been up for the whole window, and a reader
    should say "since <since>" rather than "in 24 h" until then.
    """
    now = time.time() if now is None else now
    cutoff = now - _WINDOW_S
    with _LOCK:
        recent = [(t, p) for t, p in _REJECTIONS if t >= cutoff]
        # Full AND its oldest entry still inside the window: older 401s in the
        # window were evicted, so last_24h is a lower bound.
        saturated = len(_REJECTIONS) == _REJECTIONS.maxlen and _REJECTIONS[0][0] >= cutoff
    by_path = collections.Counter(p for _, p in recent)
    since = max(cutoff, _PROCESS_START)
    return {
        "last_24h": len(recent),
        "since": _iso(since),
        "window_complete": _PROCESS_START <= cutoff,
        "last_at": _iso(recent[-1][0]) if recent else None,
        "by_path": dict(by_path.most_common(5)),
        "saturated": saturated,
    }


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(ts))


def _miner_cap(path: str) -> "int | None":
    """The miner's size cap for this file, or ``None`` when it has none.

    Uses mempalace's own rule: ``convo_miner.size_cap_for(path)`` when the
    installed mempalace provides it (so a per-format exemption there is
    honoured here without a daemon change), else ``MAX_FILE_SIZE``.
    """
    try:
        from mempalace import convo_miner
    except Exception:
        return None
    rule = getattr(convo_miner, "size_cap_for", None)
    if callable(rule):
        return rule(path)
    return getattr(convo_miner, "MAX_FILE_SIZE", None)


def transcript_over_cap(path: str) -> "tuple[int, int] | None":
    """``(size, cap)`` when ``path`` is a regular file the miner would skip, else None."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    cap = _miner_cap(path)
    if cap is None or st.st_size <= cap:
        return None
    return st.st_size, cap


SOURCE_ROOT = os.path.join(os.path.expanduser("~"), ".claude", "projects")


def resolve_source_path(path: str) -> "str | None":
    """``path`` if it is a ``.jsonl`` whose real path stays under SOURCE_ROOT, else None.

    The route takes a client-supplied path; confining it to the transcript
    tree keeps it from answering "does this file exist, how big" elsewhere.
    The drawers are keyed by the path as given, so that is what is returned.
    """
    if not os.path.isabs(path) or not path.endswith(".jsonl"):
        return None
    root = os.path.realpath(SOURCE_ROOT)
    real = os.path.realpath(path)
    if os.path.commonpath([root, real]) != root or real == root:
        return None
    return path


# Every drawer's filed_at, parsed in Python: SQL cannot compare instants
# across the formats the writers use (naive local wall time, UTC "Z",
# offsets) without casting, and one malformed value would abort a cast.
# Values that do not parse are ignored. Measured 2026-11-04 on the largest
# transcript (37,238 drawers): ~1.1 s fetch, 9 ms parse, on its own pool.
_FILED_AT_SQL = (
    "SELECT metadata->>'filed_at' FROM mempalace_drawers"
    " WHERE metadata->>'source_file' = %s"
)


# Per-path cache of the drawer side (count, newest instant). The full scan
# costs ~1.1 s on a 40K-drawer transcript and every SessionStart asks for it
# (2026-11-04). The daemon drops a path's entry after each mine of it
# (invalidate_source); the TTL bounds staleness from writers that bypass the
# daemon (a CLI mine). The file side (size, mtime, cap) is never cached, so a
# growing transcript shows a growing lag between refreshes, which is right.
_SOURCE_CACHE_TTL_S = 600
_SOURCE_CACHE_MAX = 2000
_SOURCE_CACHE: "dict[str, tuple[float, int, float | None]]" = {}
_SOURCE_LOCK = threading.Lock()


def invalidate_source(target: str) -> None:
    """Forget the cached drawer side for ``target`` and every path under it."""
    target = str(target)
    prefix = target.rstrip("/") + "/"
    with _SOURCE_LOCK:
        for key in [k for k in _SOURCE_CACHE if k == target or k.startswith(prefix)]:
            del _SOURCE_CACHE[key]


def _drawer_side(path: str, connect) -> "tuple[int, float | None, bool]":
    """``(drawers, newest_ts, cached)`` for ``path``, from the cache when fresh."""
    now = time.monotonic()
    with _SOURCE_LOCK:
        hit = _SOURCE_CACHE.get(path)
    if hit is not None and now - hit[0] < _SOURCE_CACHE_TTL_S:
        return hit[1], hit[2], True
    conn = connect()
    try:
        cur = conn.cursor()
        cur.execute("SET LOCAL statement_timeout = '5s'")
        cur.execute(_FILED_AT_SQL, (path,))
        rows = cur.fetchall()
        conn.rollback()
    finally:
        conn.close()
    candidates = [ts for ts in (_parse_ts(v) for (v,) in rows if v) if ts is not None]
    newest_ts = max(candidates) if candidates else None
    with _SOURCE_LOCK:
        if len(_SOURCE_CACHE) >= _SOURCE_CACHE_MAX and path not in _SOURCE_CACHE:
            oldest = min(_SOURCE_CACHE, key=lambda k: _SOURCE_CACHE[k][0])
            del _SOURCE_CACHE[oldest]
        _SOURCE_CACHE[path] = (now, len(rows), newest_ts)
    return len(rows), newest_ts, False


def source_status(path: str, connect) -> dict:
    """What the palace holds from one transcript, against the file itself.

    ``connect`` returns a DB-API connection. ``newest_filed_at`` is the newest
    drawer's own timestamp; ``lag_s`` is the file's mtime minus it, so a
    transcript that keeps growing while nothing new lands shows a growing lag
    no matter what any mine reported.
    """
    out: dict = {"path": path, "exists": False}
    try:
        st = os.stat(path)
    except OSError:
        st = None
    if st is not None and stat.S_ISREG(st.st_mode):
        cap = _miner_cap(path)
        out.update(
            exists=True,
            size=st.st_size,
            mtime=_iso(st.st_mtime),
            cap_bytes=cap,
            over_cap=cap is not None and st.st_size > cap,
        )
    drawers, newest_ts, cached = _drawer_side(path, connect)
    out["drawers"] = drawers
    out["cached"] = cached
    out["newest_filed_at"] = _iso(newest_ts) if newest_ts is not None else None
    out["lag_s"] = None
    if st is not None and newest_ts is not None:
        out["lag_s"] = max(0, int(st.st_mtime - newest_ts))
    return out


def _parse_ts(value: str) -> "float | None":
    from datetime import datetime

    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        # The miners write local wall-clock time without an offset.
        return time.mktime(dt.timetuple()) + dt.microsecond / 1e6
    return dt.timestamp()


# Fast-path failures (#323). Under mine load a fast SQL path's statement
# timeout was the trigger of both 2026-11-04 incidents (12:16 kg_stats, 13:11
# status): the daemon then fell back to the in-process full-corpus scan. The
# fallback is gone; this counter is the leading signal it leaves behind.
_FAST_FAILURES: "collections.deque[tuple[float, str, bool]]" = collections.deque(maxlen=10_000)
_FAST_LOCK = threading.Lock()


def is_statement_timeout(exc: BaseException) -> bool:
    """True for a postgres statement timeout (QueryCanceled), by type or text."""
    name = type(exc).__name__
    return name == "QueryCanceled" or "statement timeout" in str(exc)


def record_fast_failure(tool: str, exc: BaseException, now: float | None = None) -> None:
    with _FAST_LOCK:
        _FAST_FAILURES.append(
            (time.time() if now is None else now, str(tool)[:_PATH_MAX], is_statement_timeout(exc))
        )


def fast_failure_summary(now: float | None = None) -> dict:
    """Fast-path failures in the last 24 h; ``timeouts_24h`` is the leading signal."""
    now = time.time() if now is None else now
    cutoff = now - _WINDOW_S
    with _FAST_LOCK:
        recent = [r for r in _FAST_FAILURES if r[0] >= cutoff]
        # Full and its oldest entry inside the window: older failures in the
        # window were evicted, so the counts are lower bounds.
        saturated = len(_FAST_FAILURES) == _FAST_FAILURES.maxlen and _FAST_FAILURES[0][0] >= cutoff
    return {
        "timeouts_24h": sum(1 for r in recent if r[2]),
        "failures_24h": len(recent),
        "by_tool": dict(collections.Counter(r[1] for r in recent).most_common(5)),
        "last_at": _iso(recent[-1][0]) if recent else None,
        "since": _iso(max(cutoff, _PROCESS_START)),
        "saturated": saturated,
    }
