"""systemd watchdog + sd_notify helpers — extracted from main.py per #101.

Named ``sd_watchdog`` (not ``watchdog``) deliberately: the repo's
``watcher.py`` imports the pip ``watchdog`` package (filesystem observers),
and a module named ``watchdog.py`` on the daemon's ``sys.path`` would shadow
it and break the file watcher.

The three helpers here drive the systemd integration:

- ``sd_notify`` — fire a datagram to the ``NOTIFY_SOCKET`` (READY=1,
  WATCHDOG=1, …) with no external dependency. No-op off systemd.
- ``watchdog_interval`` — translate ``WATCHDOG_USEC`` (set by systemd when
  ``WatchdogSec=`` is configured) into a seconds budget, 0 if unset.
- ``watchdog_loop`` — the keepalive coroutine. Pings the watchdog every
  ``watchdog_tick`` seconds when this process's own loopback ``/health``
  answers. During a ``mode=rebuild`` repair (which nulls the caches for hours,
  #135) it probes ``/repair/status`` instead, and pings only while the rebuild
  keeps printing within ``rebuild_stall_s()`` (review #345 r2).

**Why the lazy ``import main`` in ``watchdog_loop``**

``watchdog_loop`` reads mutable module state that lives in ``main`` —
``_repair_state`` (mutated by the /repair handler) and ``_mp`` (the mempalace
module whose ``_get_collection`` is the health probe) — and emits via
``_sd_notify``. ``tests/test_watchdog_rebuild.py`` exercises the loop by
mutating ``main._repair_state`` and patching ``main._sd_notify`` /
``main._mp._get_collection``. Resolving those names through ``main`` *at call
time* (function-local ``import main``) keeps the patches and mutations
visible without touching the tests. Same pattern as
``fast_intercept.fast_status_payload`` (#133) and
``daemon_tools.invalidate_rooms_cache`` (#131).

main.py re-exports all three under their original ``_``-prefixed names so the
lifespan startup, the loop's own callers, and existing tests keep working
unchanged.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time


def sd_notify(msg: str) -> None:
    """Send a message to systemd notify socket without external dependencies."""
    sock_path = os.environ.get("NOTIFY_SOCKET", "")
    if not sock_path:
        return
    try:
        import socket as _sock
        with _sock.socket(_sock.AF_UNIX, _sock.SOCK_DGRAM) as s:
            # Abstract namespace sockets use NUL prefix; systemd uses @ prefix.
            addr = chr(0) + sock_path[1:] if sock_path.startswith("@") else sock_path
            s.sendto(msg.encode(), addr)
    except Exception:
        pass


def watchdog_interval() -> int:
    """Return WatchdogSec in seconds from WATCHDOG_USEC (set by systemd), or 0."""
    try:
        return int(os.environ.get("WATCHDOG_USEC", "0")) // 1_000_000
    except ValueError:
        return 0


def watchdog_tick(interval_secs: int) -> int:
    """Seconds between keepalives: a third of WatchdogSec, at most 60 s (review #345 r1: half left no margin)."""
    return max(10, min(60, interval_secs // 3))


# A served /health answer, either way: 200, or 503 when the collection cannot open (postgres down). Both prove
# the request path (accept, routing, the fast executor, the handler) is alive; a restart cannot fix the database,
# and killing on 503 would turn a postgres outage into a restart loop that StartLimitBurst then ends with the
# unit failed. A hung request path answers nothing, or not within the timeout.
SERVED = (200, 503)


async def self_health(port: int | None = None, timeout: float = 20.0, path: str = "/health",
                      api_key: str | None = None) -> int | None:
    """GET ``path`` from this process over loopback; the HTTP status, or None when nothing came back in time.

    The request goes through the same listener, event loop and handler as a client's, so a ping that depends on
    it proves the path that hung on 2026-11-19, not just that a side thread is alive (review #345 r1)."""
    port = port or int(os.environ.get("PALACE_PORT", "8085"))
    writer = None
    key = f"X-Api-Key: {api_key}\r\n" if api_key else ""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n{key}Connection: close\r\n\r\n".encode())
        await asyncio.wait_for(writer.drain(), timeout)
        status = await asyncio.wait_for(reader.readline(), timeout)
        parts = status.split()
        return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    except (OSError, asyncio.TimeoutError, ValueError):
        return None
    finally:
        if writer is not None:
            writer.close()


# How long the startup thread feeds the watchdog. A cold start measures 3-8 s; a start past 600 s is itself a
# defect worth a failed unit. palace-daemon.service's start-limit window is sized against it (review #345 r3).
STARTUP_FEED_MAX_S = 600


def startup_keepalive(interval_secs: int, max_secs: int = STARTUP_FEED_MAX_S,
                      tick: float | None = None) -> threading.Event:
    """Feed the watchdog from a plain thread while the lifespan startup runs; set the returned event to stop.

    Review #345 r1: Type=simple arms the watchdog at exec, and a long cold start is not a hang. Review r2: an
    asyncio task cannot do this, because a synchronous lifespan step (a blocking call on the event loop) starves
    it; a thread keeps feeding through one. Bounded: a startup that runs past ``max_secs`` stops being fed and
    the watchdog ends it."""
    import main
    tick = tick or watchdog_tick(interval_secs)
    stop = threading.Event()

    def feed():
        deadline = time.monotonic() + max_secs
        while not stop.is_set() and time.monotonic() < deadline:
            main._sd_notify("WATCHDOG=1\n")
            stop.wait(tick)

    threading.Thread(target=feed, name="watchdog-startup", daemon=True).start()
    return stop


# How long a rebuild may print nothing and still be fed. palace-daemon.service's start-limit window is sized
# against it (review #345 r4); PALACE_REBUILD_STALL_S can only tighten it.
REBUILD_STALL_MAX_S = 1800

# Every bound for which this module feeds the watchdog without the request path's /health: an abort cycle is
# the bound + WatchdogSec + RestartSec, and the unit's start-limit window must hold StartLimitBurst of the
# longest. tests/test_service_unit.py takes the max over this dict and fails on a *_MAX_S constant missing
# from it, so a new feed bound cannot land without its window.
FEED_BOUNDS_S = {"running": 0, "startup": STARTUP_FEED_MAX_S, "rebuild": REBUILD_STALL_MAX_S}


def rebuild_stall_s() -> int:
    """How long a rebuild may print nothing before the watchdog stops feeding it (review #345 r2).

    Not two ticks: rebuild_index prints one line per 5000-drawer batch (minutes apart at its measured
    ~11 drawers/s) and nothing at all while it extracts, so a short window would kill a healthy rebuild.
    ``PALACE_REBUILD_STALL_S`` may lower it, never raise it past ``REBUILD_STALL_MAX_S`` (review #345 r4)."""
    try:
        return max(60, min(REBUILD_STALL_MAX_S, int(os.environ.get("PALACE_REBUILD_STALL_S", REBUILD_STALL_MAX_S))))
    except ValueError:
        return REBUILD_STALL_MAX_S


def rebuild_quiet_s(progress, now: float | None = None) -> float:
    """Seconds since the rebuild last printed (its start, before the first line); 0 before progress exists."""
    if not isinstance(progress, dict):
        return 0.0
    last = progress.get("beat_at") or progress.get("started_at_monotonic")
    if not isinstance(last, (int, float)):
        return 0.0
    return max(0.0, (time.monotonic() if now is None else now) - last)


async def watchdog_loop(interval_secs: int) -> None:
    """Ping systemd's watchdog every ``watchdog_tick`` seconds, only after this process served its own /health.

    Honor CancelledError so the lifespan shutdown can stop us cleanly —
    otherwise uvicorn hangs on "Waiting for background tasks to complete"
    until systemd SIGKILLs at TimeoutStopSec.
    """
    import main  # lazy — preserves patch.object(main, "_sd_notify") and live mutation
    #              of main._repair_state in tests/test_watchdog_rebuild.py.
    tick = watchdog_tick(interval_secs)
    while True:
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            return
        # During mode=rebuild, /health is the wrong probe: a rebuild holds _exclusive_palace() with the
        # client/collection caches nulled (see the /repair handler), so _get_collection() can return None or
        # block for the whole operation, and a kill mid-rebuild is the most destructive kind (#135). Review
        # #345 r2: an unconditional ping let a daemon hung mid-rebuild live forever. So during a rebuild the
        # ping needs both (a) the rebuild printed within rebuild_stall_s(), and (b) the request path answers
        # /repair/status, which reads _repair_state only and never touches the collection.
        if main._repair_state.get("in_progress") and main._repair_state.get("mode") == "rebuild":
            quiet = rebuild_quiet_s(main._repair_state.get("progress"))
            if quiet > rebuild_stall_s():
                main._log.warning("Watchdog: the rebuild printed nothing for %ds: skipping WATCHDOG=1", int(quiet))
                continue
            status = await self_health(timeout=max(5.0, tick / 3), path="/repair/status",
                                       api_key=os.environ.get("PALACE_API_KEY"))
            if status is not None:
                main._sd_notify("WATCHDOG=1\n")
            else:
                main._log.warning("Watchdog: /repair/status did not answer during the rebuild: skipping WATCHDOG=1")
            continue
        try:
            # /health runs its probes on the FAST pool (#286), so a default pool starved by MCP tool execution does
            # not stop WATCHDOG=1; a stuck loop, listener or fast pool does.
            status = await self_health(timeout=max(5.0, tick / 3))
            if status in SERVED:
                main._sd_notify("WATCHDOG=1\n")
            else:
                main._log.warning("Watchdog: /health did not answer (%s): skipping WATCHDOG=1", status)
        except Exception as e:
            main._log.warning("Watchdog check failed: %s", e)
