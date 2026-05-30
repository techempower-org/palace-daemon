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


async def watchdog_loop(interval_secs: int) -> None:
    """Ping systemd watchdog at half the watchdog interval, only when palace is healthy.

    Honor CancelledError so the lifespan shutdown can stop us cleanly —
    otherwise uvicorn hangs on "Waiting for background tasks to complete"
    until systemd SIGKILLs at TimeoutStopSec.
    """
    import main  # lazy — preserves patch.object(main, "_sd_notify") /
    #              patch.object(main._mp, "_get_collection") and live mutation
    #              of main._repair_state in tests/test_watchdog_rebuild.py.
    tick = max(10, interval_secs // 2)
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
            loop = asyncio.get_running_loop()
            col = await loop.run_in_executor(None, main._mp._get_collection)
            if col is not None:
                main._sd_notify("WATCHDOG=1\n")
            else:
                main._log.warning("Watchdog: palace collection unavailable — skipping WATCHDOG=1")
        except Exception as e:
            main._log.warning("Watchdog check failed: %s", e)
