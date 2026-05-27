"""Tests for the watchdog keepalive-during-rebuild fix.

``_watchdog_loop`` is health-gated: it withholds ``WATCHDOG=1`` whenever the
palace ``_get_collection()`` probe returns None or throws, so a genuinely
wedged daemon gets killed by systemd. But a ``mode=rebuild`` repair holds
``_exclusive_palace()`` with the client/collection caches nulled for the whole
(6-9h) operation, so that probe would return None and starve the keepalive —
SIGABRT-ing the daemon mid-rebuild.

The fix: during ``in_progress + mode=rebuild``, send ``WATCHDOG=1``
unconditionally and skip the probe entirely. Outside a rebuild, the original
health-gated behavior is preserved.

These drive exactly one loop iteration by stubbing ``asyncio.sleep`` to return
once and then raise ``CancelledError`` (which the loop catches to exit). No
live daemon, palace, or systemd socket required — ``_sd_notify`` is mocked.

Run with::

    python -m unittest tests.test_watchdog_rebuild -v
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


def _one_iteration_sleep():
    """An asyncio.sleep stand-in: first await returns, second cancels the loop.

    Lets _watchdog_loop run exactly one body iteration, then exit via the
    CancelledError its own ``try/except`` around sleep swallows with a return.
    """
    calls = {"n": 0}

    async def _sleep(_secs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        raise asyncio.CancelledError()

    return _sleep


class TestWatchdogRebuild(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._orig_repair = dict(main._repair_state)

    async def asyncTearDown(self):
        main._repair_state.clear()
        main._repair_state.update(self._orig_repair)

    async def test_rebuild_sends_keepalive_without_probing(self):
        main._repair_state["in_progress"] = True
        main._repair_state["mode"] = "rebuild"
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(main._mp, "_get_collection") as probe:
            await main._watchdog_loop(20)
        # Keepalive fed, probe skipped entirely.
        notify.assert_called_once_with("WATCHDOG=1\n")
        probe.assert_not_called()

    async def test_healthy_outside_rebuild_probes_and_pings(self):
        main._repair_state["in_progress"] = False
        main._repair_state["mode"] = None
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(main._mp, "_get_collection", return_value=MagicMock()) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_called_once_with("WATCHDOG=1\n")

    async def test_unavailable_outside_rebuild_withholds_keepalive(self):
        """The health-gate still bites outside a rebuild: a None collection
        means the daemon is wedged, so we deliberately let systemd kill it."""
        main._repair_state["in_progress"] = False
        main._repair_state["mode"] = None
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(main._mp, "_get_collection", return_value=None) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_not_called()

    async def test_non_rebuild_repair_still_health_gated(self):
        """A non-rebuild repair (e.g. light/scan/prune) does NOT get the
        unconditional keepalive — only mode=rebuild nulls the caches, so
        other modes keep the normal probe."""
        main._repair_state["in_progress"] = True
        main._repair_state["mode"] = "light"
        with patch("asyncio.sleep", side_effect=_one_iteration_sleep()), \
             patch.object(main, "_sd_notify") as notify, \
             patch.object(main._mp, "_get_collection", return_value=MagicMock()) as probe:
            await main._watchdog_loop(20)
        probe.assert_called_once()
        notify.assert_called_once_with("WATCHDOG=1\n")


if __name__ == "__main__":
    unittest.main()
