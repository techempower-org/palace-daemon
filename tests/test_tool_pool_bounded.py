"""A full MCP tool pool must say "busy" now, not queue forever (#286).

The 2026-09-17 outage's defining detail: the access log showed **no /mcp
traffic for the last 20 minutes**. The work outlived every caller. Hooks with
2-5 s budgets had long since timed out, and the daemon was still executing
their queued kg_stats calls, each taking the KG lock and running a 10-20 s
full-graph walk.

`PALACE_MCP_TOOL_TIMEOUT_SECONDS` did not help, and the code says why in its
own comment: `asyncio.wait_for` cancels the *awaitable*, but the thread keeps
running. The caller gets an error envelope and the executor slot stays
occupied. So the existing timeout bounds the CALLER's wait and not the
daemon's work — which is the opposite of what was needed.

Queueing work for a caller that is already gone is never right. When the pool
is full the honest answer is an immediate "busy", so the client can retry or
give up on its own schedule.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_tool_pool_bounded.py -q
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


def _req(tool="mempalace_search", args=None):
    return {
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {"query": "x"}},
    }


class BoundedToolPoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._release = threading.Event()
        self._entered = threading.Semaphore(0)
        main._tool_inflight = 0
        self._bound = patch.object(main, "PALACE_MCP_TOOL_MAX_INFLIGHT", 2)
        self._bound.start()

    async def asyncTearDown(self):
        self._release.set()
        if getattr(self, "_stuck_patch", None) is not None:
            self._stuck_patch.stop()
            self._stuck_patch = None
        self._bound.stop()
        main._tool_inflight = 0

    def _stuck(self, *_a, **_k):
        self._entered.release()
        self._release.wait(timeout=30)
        return {"jsonrpc": "2.0", "id": 1, "result": {}}

    async def _fill(self):
        """Occupy every tool slot; returns the in-flight tasks.

        The bound is patched BELOW the read semaphore (4) — otherwise the
        semaphore caps concurrency first and the bound is unreachable, which
        is exactly how the first version of this guard was dead code.
        """
        n = main.PALACE_MCP_TOOL_MAX_INFLIGHT
        # Started, not `with`: the tasks outlive this function, and a context
        # manager would restore handle_request before they even submit.
        self._stuck_patch = patch.object(main._mp, "handle_request", side_effect=self._stuck)
        self._stuck_patch.start()
        tasks = [asyncio.create_task(main._call(_req())) for _ in range(n)]
        # Positive control: the pool is genuinely full before we assert that
        # the next call is rejected. The wait must YIELD — a blocking
        # Semaphore.acquire() here stalls the event loop, so the tasks never
        # start and the control reports "never filled" against working code.
        for _ in range(n):
            self.assertTrue(await self._await_entry(), "pool never filled")
        return tasks

    async def _await_entry(self, timeout=10.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if self._entered.acquire(blocking=False):
                return True
            await asyncio.sleep(0.02)
        return False

    async def test_a_call_past_the_bound_is_refused_immediately(self):
        tasks = await self._fill()

        result = await asyncio.wait_for(main._call(_req()), timeout=3)

        self.assertIn("error", result)
        self.assertEqual(result["error"]["code"], -32003)
        self.assertIn("busy", result["error"]["message"].lower())

        self._release.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def test_calls_within_the_bound_are_not_refused(self):
        """Control: the bound must not reject normal traffic."""
        ok = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        with patch.object(main._mp, "handle_request", return_value=ok):
            result = await asyncio.wait_for(main._call(_req()), timeout=5)

        self.assertNotIn("error", result)

    async def test_slots_are_released_after_a_call_completes(self):
        ok = {"jsonrpc": "2.0", "id": 3, "result": {}}
        with patch.object(main._mp, "handle_request", return_value=ok):
            for _ in range(main.PALACE_MCP_TOOL_MAX_INFLIGHT + 3):
                r = await asyncio.wait_for(main._call(_req()), timeout=5)
                self.assertNotIn("error", r)
        self.assertEqual(main._tool_inflight, 0)

    async def test_slots_are_released_even_when_the_tool_raises(self):
        with patch.object(main._mp, "handle_request", side_effect=RuntimeError("boom")):
            r = await asyncio.wait_for(main._call(_req()), timeout=5)
        self.assertIn("error", r)
        self.assertEqual(main._tool_inflight, 0, "a raising tool must not leak a slot")

    async def test_the_slow_path_logs_the_tool_and_an_arguments_hash(self):
        """The daemon log carried nothing that pointed at this — py-spy did."""
        ok = {"jsonrpc": "2.0", "id": 3, "result": {}}
        with patch.object(main._mp, "handle_request", return_value=ok):
            with self.assertLogs(main._log, level=logging.INFO) as cap:
                await main._call(_req("mempalace_kg_stats", {"wing": "2g"}))

        line = "\n".join(cap.output)
        self.assertIn("mempalace_kg_stats", line)
        self.assertIn("args=", line, "an arguments hash must identify repeat callers")

    async def test_the_args_hash_is_stable_and_distinguishes_arguments(self):
        a = main._args_hash({"wing": "2g"})
        b = main._args_hash({"wing": "2g"})
        c = main._args_hash({"wing": "other"})
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertEqual(main._args_hash({"a": 1, "b": 2}), main._args_hash({"b": 2, "a": 1}))


    async def test_a_timed_out_call_keeps_its_slot_until_the_thread_finishes(self):
        """The mechanism of the outage, pinned.

        `asyncio.wait_for` cancels the awaitable and releases the read
        semaphore, but the worker thread keeps running. If the in-flight slot
        were released on the await returning, a stream of timeouts would let
        threads accumulate without bound — which is how py-spy found SEVEN
        threads in stats() against a read-concurrency limit of 4.
        """
        with patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.2), \
             patch.object(main._mp, "handle_request", side_effect=self._stuck):
            r = await asyncio.wait_for(main._call(_req()), timeout=5)

        self.assertIn("error", r)
        self.assertEqual(r["error"]["code"], -32001, "should be the timeout envelope")
        # The caller is gone; the thread is not. The slot must still be held.
        self.assertEqual(main._tool_inflight, 1, "a timeout must NOT free the slot")

        self._release.set()
        for _ in range(100):
            if main._tool_inflight == 0:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(main._tool_inflight, 0, "slot must free when the thread ends")

if __name__ == "__main__":
    unittest.main()
