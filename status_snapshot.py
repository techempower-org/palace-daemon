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
- ``warm()`` starts the first computation at daemon startup, so even the
  first session after a restart is served from memory.
"""

from __future__ import annotations

import logging
import threading
import time

_log = logging.getLogger("palace-daemon")


class SnapshotCache:
    def __init__(self, compute, ttl_s: float = 60.0, max_age_s: float = 900.0):
        self._compute = compute
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
        """``(payload, age_s)``. Blocks only when no usable snapshot exists."""
        now = time.monotonic()
        with self._lock:
            value, at, stale = self._value, self._at, self._stale
        if value is not None and now - at < self.max_age_s:
            if stale or now - at >= self.ttl_s:
                self._start_refresh()
            return value, now - at
        with self._sync:
            # Another caller may have filled it while this one waited.
            with self._lock:
                if self._value is not None and time.monotonic() - self._at < self.max_age_s:
                    return self._value, time.monotonic() - self._at
            gen = self._generation()
            value = self._compute()
            self._store(value, gen)
            return value, 0.0

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
                self._store(self._compute(), gen)
        except Exception:
            # Keep serving the last good snapshot; max_age_s bounds how long.
            _log.exception("status snapshot refresh failed; serving the previous one")
        finally:
            with self._lock:
                self._refreshing = False
