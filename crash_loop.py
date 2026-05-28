"""Crash-loop detection — extracted from main.py per #101 (eighth slice).

Tracks daemon restart timestamps in a JSON file under
``~/.cache/palace-daemon/restart_history.json`` and reports degraded
status via ``/health`` when more than ``PALACE_CRASH_LOOP_THRESHOLD_COUNT``
restarts happen in ``PALACE_CRASH_LOOP_THRESHOLD_SECONDS``.

Auto-recovery: once the daemon has been running continuously for
``PALACE_CRASH_LOOP_RECOVERY_SECONDS`` (default 30 min), the crash-loop
flag suppresses itself even if old restarts are still in the window.
This lets the daemon self-heal after a deploy session without operator
intervention.

main.py re-exports the names under their original ``_``-prefixed form
so call sites in the lifespan handler and the ``/health`` endpoint keep
working without edits.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path


CRASH_LOOP_DIR = Path.home() / ".cache" / "palace-daemon"
RESTART_HISTORY_PATH = CRASH_LOOP_DIR / "restart_history.json"
CRASH_LOOP_WINDOW = int(os.getenv("PALACE_CRASH_LOOP_THRESHOLD_SECONDS", "600"))
CRASH_LOOP_THRESHOLD = int(os.getenv("PALACE_CRASH_LOOP_THRESHOLD_COUNT", "3"))
CRASH_LOOP_RECOVERY = int(os.getenv("PALACE_CRASH_LOOP_RECOVERY_SECONDS", "1800"))

# Monotonic timestamp captured at module-load time. Used by
# crash_loop_state() to auto-exit the degraded state after the daemon
# has been running cleanly for CRASH_LOOP_RECOVERY seconds.
#
# Because main.py imports this module at startup, this captures the
# daemon's startup moment as expected — module-import order makes the
# extracted name equivalent to the previous in-main definition.
STARTUP_MONOTONIC: float = time.monotonic()


def record_restart() -> None:
    """Append now() to the restart history, pruning entries older than the window."""
    CRASH_LOOP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        data = json.loads(RESTART_HISTORY_PATH.read_text()) if RESTART_HISTORY_PATH.exists() else {}
    except Exception:
        data = {}
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=CRASH_LOOP_WINDOW)
    restarts = [r for r in data.get("restarts", []) if datetime.fromisoformat(r) > cutoff]
    restarts.append(now.isoformat())
    RESTART_HISTORY_PATH.write_text(json.dumps({"restarts": restarts}))


def crash_loop_state() -> dict:
    """Return current crash-loop status: {crash_loop, restart_count, ...}."""
    try:
        data = json.loads(RESTART_HISTORY_PATH.read_text()) if RESTART_HISTORY_PATH.exists() else {}
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=CRASH_LOOP_WINDOW)
        recent = [r for r in data.get("restarts", []) if datetime.fromisoformat(r) > cutoff]
        in_loop = len(recent) >= CRASH_LOOP_THRESHOLD

        # Auto-recovery: if the daemon has been running continuously for
        # CRASH_LOOP_RECOVERY seconds without crashing, suppress the
        # crash-loop flag even if old restarts are still in the window.
        # This lets the daemon self-heal without operator intervention.
        uptime = time.monotonic() - STARTUP_MONOTONIC
        recovered = in_loop and uptime >= CRASH_LOOP_RECOVERY

        return {
            "crash_loop": in_loop and not recovered,
            "restart_count": len(recent),
            "window_seconds": CRASH_LOOP_WINDOW,
            "uptime_seconds": round(uptime, 1),
            "recovered": recovered,
        }
    except Exception:
        return {"crash_loop": False, "restart_count": 0, "window_seconds": CRASH_LOOP_WINDOW}
