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
- ``watchdog_loop`` — the keepalive coroutine. Pings the watchdog at half
  the interval, health-gated on the palace collection probe, except during a
  ``mode=rebuild`` repair where it feeds the watchdog unconditionally (the
  rebuild nulls the caches for hours; the gate would otherwise starve the
  keepalive and let systemd SIGABRT the daemon mid-rebuild — #135 / the
  test_watchdog_rebuild.py rationale).

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


async def self_health(port: int | None = None, timeout: float = 20.0) -> int | None:
    """GET /health from this process over loopback; the HTTP status, or None when nothing came back in time.

    The request goes through the same listener, event loop and handler as a client's, so a ping that depends on
    it proves the path that hung on 2026-11-19, not just that a side thread is alive (review #345 r1)."""
    port = port or int(os.environ.get("PALACE_PORT", "8085"))
    writer = None
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout)
        writer.write(b"GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        await asyncio.wait_for(writer.drain(), timeout)
        status = await asyncio.wait_for(reader.readline(), timeout)
        parts = status.split()
        return int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
    except (OSError, asyncio.TimeoutError, ValueError):
        return None
    finally:
        if writer is not None:
            writer.close()


async def startup_keepalive(interval_secs: int, max_secs: int = 1800) -> None:
    """Feed the watchdog while the lifespan startup runs (review #345 r1: Type=simple arms the watchdog at exec,
    and a legitimately long start, such as a cold index load, must not be killed). Bounded: a startup that hangs
    past ``max_secs`` stops being fed and the watchdog ends it. Cancelled by the lifespan once startup is done."""
    import main
    tick = watchdog_tick(interval_secs)
    waited = 0
    while waited < max_secs:
        main._sd_notify("WATCHDOG=1\n")
        try:
            await asyncio.sleep(tick)
        except asyncio.CancelledError:
            return
        waited += tick


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
        # During mode=rebuild, send the keepalive unconditionally and skip the
        # probe. A rebuild holds _exclusive_palace() with the client/collection
        # caches nulled (see the /repair handler), so _get_collection() can
        # return None or block for the whole 6-9h operation. The health-gate
        # below would then withhold WATCHDOG=1 and systemd would SIGABRT the
        # daemon mid-rebuild — exactly when a kill is most destructive. Keep
        # feeding the watchdog; the rebuild is a known long-running operation
        # we want to run to completion.
        if main._repair_state.get("in_progress") and main._repair_state.get("mode") == "rebuild":
            main._sd_notify("WATCHDOG=1\n")
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
