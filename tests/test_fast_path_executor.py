"""/health and the fast routes must not share a thread pool with MCP tools (#286).

/health already carried the comment "Bypass semaphores — health must respond
even when all slots are busy". It did bypass the semaphores. It did NOT bypass
the executor, and the executor is what ran out.

During the 2026-09-17 outage seven `mempalace_kg_stats` calls sat on the
shared default `ThreadPoolExecutor` waiting on the KG lock. /health,
/status/fast, /search/fast and the systemd watchdog probe all reach it via
`run_in_executor(None, ...)`, so every one of them blocked while /mine/status
(which uses no executor) answered instantly — the daemon looked dead to every
health check for 40 minutes. The watchdog case is the sharpest: a starved pool
means systemd stops receiving WATCHDOG=1, so a stuck tool can escalate into a
SIGABRT of a daemon that is otherwise fine.

The protection existed, was written down, and guarded the wrong resource.

These tests hold a synthetic stuck tool on the TOOL pool and require the fast
paths to keep answering.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_fast_path_executor.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class FastExecutorIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._release = threading.Event()
        self._entered = threading.Semaphore(0)

    async def asyncTearDown(self):
        self._release.set()

    def _stuck(self, *_a, **_k):
        """A tool that blocks exactly as `stats()` did, on a lock it never gets."""
        self._entered.release()
        self._release.wait(timeout=30)
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    async def test_a_dedicated_fast_executor_exists_and_is_not_the_default(self):
        self.assertIsNotNone(getattr(main, "_FAST_EXECUTOR", None))
        self.assertIsNotNone(getattr(main, "_TOOL_EXECUTOR", None))
        self.assertIsNot(main._FAST_EXECUTOR, main._TOOL_EXECUTOR)

    async def test_health_answers_while_the_tool_pool_is_fully_stuck(self):
        """The acceptance test from #286: N stuck tool calls, /health still answers."""
        n = main._TOOL_EXECUTOR._max_workers
        loop = asyncio.get_running_loop()

        stuck = [
            loop.run_in_executor(main._TOOL_EXECUTOR, self._stuck)
            for _ in range(n)
        ]
        # Positive control: every worker is genuinely occupied before we assert
        # anything about /health. Without this the test could pass because the
        # tools had not started yet.
        for _ in range(n):
            self.assertTrue(self._entered.acquire(timeout=10), "tool pool never filled")

        with patch.object(main, "_check_auth", side_effect=lambda *_a, **_k: None), \
             patch.object(main._mp, "handle_request", return_value={"result": {}}), \
             patch.object(main._mp, "_get_collection", return_value=object()):
            t0 = time.monotonic()
            resp = await asyncio.wait_for(main.health(), timeout=5)
            elapsed = time.monotonic() - t0

        self.assertLess(elapsed, 5, "/health blocked behind the stuck tool pool")
        self.assertIsNotNone(resp)

        self._release.set()
        await asyncio.gather(*stuck, return_exceptions=True)

    async def test_the_watchdog_probe_uses_the_fast_executor(self):
        """A starved pool must not stop WATCHDOG=1 — systemd SIGABRTs for that."""
        import inspect

        import sd_watchdog

        src = inspect.getsource(sd_watchdog.watchdog_loop)
        self.assertIn("_FAST_EXECUTOR", src)
        self.assertNotIn("run_in_executor(None", src)

    async def test_the_fast_routes_do_not_use_the_default_executor(self):
        """status_fast / search_fast / health must name the fast pool."""
        import inspect

        for fn in (main.health, main.status_fast, main.search_fast):
            src = inspect.getsource(fn)
            self.assertNotIn(
                "run_in_executor(None", src,
                f"{fn.__name__} still shares the default pool with MCP tools",
            )
            self.assertIn("_FAST_EXECUTOR", src, f"{fn.__name__} must use the fast pool")


if __name__ == "__main__":
    unittest.main()
