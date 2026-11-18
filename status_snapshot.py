"""A stale-while-revalidate snapshot for /status/fast.

``fast_status_payload`` counts the drawer table three ways (total, per wing,
per room); measured 2026-11-04 at 1.33M drawers, each count is 370-500 ms,
so /status/fast took 1.0-1.4 s, and every SessionStart across the fleet calls
it. The counts only need to be roughly current for a status line, so:

- a request is answered from the snapshot whenever one exists that is younger
  than ``max_age_s``; if it is older than ``ttl_s`` or was invalidated (a mine
  finished), the same request starts ONE background refresh and still returns
  at once;
- only a request with no usable snapshot (first call, or past ``max_age_s``)
  waits for the computation, and concurrent such requests share one;
- past ``max_age_s``, if that synchronous computation fails, the request is
  answered with the last good snapshot and its true age (``get()`` returns it;
  the route marks it ``snapshot_stale``) and a background refresh starts:
  a status endpoint never answers 500 because one query was slow while it
  holds an answer. Requests queued behind the failed attempt do not each
  retry it. It raises only when no snapshot was ever computed;
- the background refresh may use its own ``refresh_compute`` (main.py gives
  it a 30 s statement timeout; the synchronous path keeps 3 s), since no
  request waits on it.
- ``warm()`` starts the first computation at daemon startup, so even the
  first session after a restart is served from memory.

2026-11-17 20:00-22:0x in production: under I/O load every refresh hit the 3 s
statement timeout; after max_age_s every /status/fast recomputed, timed out
and answered 500 to every host's SessionStart line.
"""

from __future__ import annotations

import logging
import threading
import time

_log = logging.getLogger("palace-daemon")


class SnapshotCache:
    def __init__(self, compute, ttl_s: float = 60.0, max_age_s: float = 900.0, on_failure=None,
                 refresh_compute=None):
        self._compute = compute
        # the background refresh's computation; no request waits on it, so it may run longer
        self._refresh_compute = refresh_compute if refresh_compute is not None else compute
        self._on_failure = on_failure  # called with the exception of a failed refresh
        self._failed_at = 0.0  # monotonic time the last synchronous computation failed
        self.ttl_s = ttl_s
        self.max_age_s = max_age_s
        self._lock = threading.Lock()  # guards the fields below
        self._value = None
        self._at = 0.0  # monotonic time the value was computed
        self._stale = False
        self._refreshing = False
        self._gen = 0  # bumped by invalidate(); a refresh begun before it stays stale
        self._sync = threading.Lock()  # one synchronous computation at a time

    def invalidate(self) -> None:
        """Mark the snapshot stale; the next request refreshes it in the background."""
        with self._lock:
            self._stale = True
            self._gen += 1

    def warm(self) -> None:
        """Compute the first snapshot in the background (daemon startup)."""
        self._start_refresh()

    def get(self) -> "tuple[dict, float]":
        """``(payload, age_s)``. Blocks only when no usable snapshot exists.

        Past ``max_age_s`` a failed recomputation answers with the last good snapshot and its
        age (``age_s >= max_age_s`` tells the caller it is stale); it raises only when there is none.
        """
        now = time.monotonic()
        with self._lock:
            value, at, stale = self._value, self._at, self._stale
        if value is not None and now - at < self.max_age_s:
            if stale or now - at >= self.ttl_s:
                self._start_refresh()
            return value, now - at
        with self._sync:
            with self._lock:
                # Another caller may have filled it while this one waited.
                if self._value is not None and time.monotonic() - self._at < self.max_age_s:
                    return self._value, time.monotonic() - self._at
                # Or tried and failed while this one waited: do not queue another attempt.
                if self._value is not None and self._failed_at > now:
                    return self._value, time.monotonic() - self._at
            gen = self._generation()
            try:
                value = self._compute()
            except Exception as e:
                with self._lock:
                    self._failed_at = time.monotonic()
                    last, last_at = self._value, self._at
                if last is None:
                    raise
                _log.warning("status snapshot recompute failed (%s); serving the last good one, %.0f s old",
                             type(e).__name__, time.monotonic() - last_at)
                self._notify_failure(e)
                # Safe while holding _sync: this only spawns the thread, which takes _sync after us.
                self._start_refresh()
                return last, time.monotonic() - last_at
            self._store(value, gen)
            return value, 0.0

    def _notify_failure(self, e: Exception) -> None:
        if self._on_failure is not None:
            try:
                self._on_failure(e)
            except Exception:
                pass

    def _generation(self) -> int:
        with self._lock:
            return self._gen

    def _store(self, value, gen: int) -> None:
        with self._lock:
            self._value = value
            self._at = time.monotonic()
            # An invalidate() during the computation means a mine finished
            # after it began: keep the flag so the next request refreshes.
            self._stale = gen != self._gen

    def _start_refresh(self) -> None:
        with self._lock:
            if self._refreshing:
                return
            self._refreshing = True
        threading.Thread(target=self._refresh, name="status-snapshot", daemon=True).start()

    def _refresh(self) -> None:
        try:
            with self._sync:
                gen = self._generation()
                self._store(self._refresh_compute(), gen)
        except Exception as e:
            # Keep serving the last good snapshot (marked stale past max_age_s).
            _log.exception("status snapshot refresh failed; serving the previous one")
            self._notify_failure(e)
        finally:
            with self._lock:
                self._refreshing = False
