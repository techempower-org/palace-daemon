"""The hook's diary_write has its own lane, so hung reads cannot starve saves (#323).

2026-11-04 12:16-12:37 on familiar: hung graph reads (graph_stats, kg_stats on
its slow-path fallback, search) held all eight PALACE_MCP_TOOL_MAX_INFLIGHT
slots, and every lane's Stop/PreCompact diary_write was refused as "busy" for
20 minutes. Saves now count against PALACE_MCP_SAVE_MAX_INFLIGHT and run on
_SAVE_EXECUTOR; reads never touch either.

Run with::

    python -m pytest tests/test_save_lane.py -q
"""
from __future__ import annotations

import asyncio
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


def _req(tool, args=None):
    return {
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": tool, "arguments": args or {"x": 1}},
    }


class _LaneHarness(unittest.IsolatedAsyncioTestCase):
    """Setup and helpers only; the test classes below add the cases."""

    async def asyncSetUp(self):
        self._release = threading.Event()
        self._entered = threading.Semaphore(0)
        main._tool_inflight = 0
        main._save_inflight = 0
        # Bounds below the read (4) and write (2) semaphores, so the bound is
        # what refuses, not a semaphore that waits.
        self._patches = [
            patch.object(main, "PALACE_MCP_TOOL_MAX_INFLIGHT", 2),
            patch.object(main, "PALACE_MCP_SAVE_MAX_INFLIGHT", 1),
        ]
        for p in self._patches:
            p.start()
        # Fresh semaphores per test (bound to this test's loop).
        self._sems = [
            patch.object(main, "_save_sem", asyncio.Semaphore(2)),
            patch.object(main, "_write_sem", asyncio.Semaphore(2)),
            patch.object(main, "_read_sem", asyncio.Semaphore(4)),
        ]
        for p in self._sems:
            p.start()
        self._handler = None
        self._tasks = []

    async def asyncTearDown(self):
        self._release.set()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        # Let parked worker threads finish and their release callbacks land
        # before resetting, so a leak shows up here rather than in a later test.
        for _ in range(250):
            if main._tool_inflight == 0 and main._save_inflight == 0:
                break
            await asyncio.sleep(0.02)
        if self._handler is not None:
            self._handler.stop()
        for p in self._patches + self._sems:
            p.stop()
        main._tool_inflight = 0
        main._save_inflight = 0

    def _spawn(self, coro):
        t = asyncio.create_task(coro)
        self._tasks.append(t)
        return t

    def _route(self, request_dict):
        """Reads park until released; a save returns at once unless told to park."""
        name = request_dict["params"]["name"]
        if name in main._SAVE_TOOLS and not self.park_saves:
            return {"jsonrpc": "2.0", "id": 7, "result": {"saved": True}}
        self._entered.release()
        self._release.wait(timeout=30)
        return {"jsonrpc": "2.0", "id": 7, "result": {}}

    def _install(self, park_saves=False):
        self.park_saves = park_saves
        self._handler = patch.object(main._mp, "handle_request", side_effect=self._route)
        self._handler.start()

    async def _wait_entries(self, n, timeout=10.0):
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        got = 0
        while got < n and loop.time() < deadline:
            if self._entered.acquire(blocking=False):
                got += 1
            else:
                await asyncio.sleep(0.02)
        self.assertEqual(got, n, "calls never parked")


class SaveLaneTests(_LaneHarness):
    async def test_a_save_succeeds_while_reads_hold_every_shared_slot(self):
        self._install()
        reads = [asyncio.create_task(main._call(_req("mempalace_graph_stats")))
                 for _ in range(main.PALACE_MCP_TOOL_MAX_INFLIGHT)]
        await self._wait_entries(len(reads))
        # Positive control: the shared pool is genuinely full.
        busy = await asyncio.wait_for(main._call(_req("mempalace_search")), timeout=3)
        self.assertEqual(busy["error"]["code"], -32003)

        saved = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
        self.assertEqual(saved.get("result"), {"saved": True}, saved)

        self._release.set()
        await asyncio.gather(*reads, return_exceptions=True)

    async def test_saves_are_bounded_by_their_own_lane(self):
        self._install(park_saves=True)
        first = asyncio.create_task(main._call(_req("mempalace_diary_write")))
        await self._wait_entries(1)
        second = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=3)
        self.assertEqual(second["error"]["code"], -32003)
        self.assertIn("PALACE_MCP_SAVE_MAX_INFLIGHT", second["error"]["message"])
        self.assertEqual(main._tool_inflight, 0, "a save took a shared slot")
        self._release.set()
        await asyncio.gather(first, return_exceptions=True)

    async def test_parked_saves_leave_the_shared_lane_free_for_reads(self):
        self._install(park_saves=True)
        save = asyncio.create_task(main._call(_req("mempalace_diary_write")))
        await self._wait_entries(1)
        read = asyncio.create_task(main._call(_req("mempalace_search")))
        await self._wait_entries(1)  # the read got a thread and a slot
        self.assertEqual((main._save_inflight, main._tool_inflight), (1, 1))
        self._release.set()
        await asyncio.gather(save, read, return_exceptions=True)

    async def test_save_slots_are_released_after_completion(self):
        self._install()
        for _ in range(main.PALACE_MCP_SAVE_MAX_INFLIGHT + 3):
            r = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
            self.assertNotIn("error", r)
        for _ in range(50):
            if main._save_inflight == 0:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(main._save_inflight, 0)

    async def test_saves_run_on_their_own_executor(self):
        seen = []

        def _record(request_dict):
            seen.append(threading.current_thread().name)
            return {"jsonrpc": "2.0", "id": 7, "result": {}}

        with patch.object(main._mp, "handle_request", side_effect=_record):
            await main._call(_req("mempalace_diary_write"))
            await main._call(_req("mempalace_search"))
        self.assertTrue(seen[0].startswith("palace-save"), seen)
        self.assertTrue(seen[1].startswith("palace-tool"), seen)


class IncidentShapedTests(_LaneHarness):
    """Production bounds: 8 shared slots, 2 save slots, 2 save permits."""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        for p in self._patches:
            p.stop()
        self._patches = [
            patch.object(main, "PALACE_MCP_TOOL_MAX_INFLIGHT", 8),
            patch.object(main, "PALACE_MCP_SAVE_MAX_INFLIGHT", 2),
            patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.2),
        ]
        for p in self._patches:
            p.start()

    async def test_eight_timed_out_reads_hold_every_shared_thread_and_a_save_still_lands(self):
        # The 12:16-12:37 shape: each read's caller times out (semaphore
        # released) while its thread stays parked, so all 8 slots and all 8
        # _TOOL_EXECUTOR threads end up held.
        self._install()
        for _ in range(8):
            r = await asyncio.wait_for(main._call(_req("mempalace_graph_stats")), timeout=5)
            self.assertEqual(r["error"]["code"], -32001)
        await self._wait_entries(8)
        self.assertEqual(main._tool_inflight, 8)
        refused = await asyncio.wait_for(main._call(_req("mempalace_search")), timeout=3)
        self.assertEqual(refused["error"]["code"], -32003)  # positive control

        saved = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
        self.assertEqual(saved.get("result"), {"saved": True}, saved)

    async def test_two_hung_saves_time_out_keep_their_slots_and_the_third_is_refused(self):
        self._install(park_saves=True)
        for _ in range(2):
            r = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
            self.assertEqual(r["error"]["code"], -32001)
        await self._wait_entries(2)
        self.assertEqual(main._save_inflight, 2, "a timed-out save freed its slot early")
        third = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=3)
        self.assertEqual(third["error"]["code"], -32003)
        self.assertIn("PALACE_MCP_SAVE_MAX_INFLIGHT", third["error"]["message"])

    async def test_other_writes_holding_the_write_semaphore_do_not_block_saves(self):
        self._install()
        with patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0):
            writes = [self._spawn(main._call(_req("mempalace_add_drawer"))) for _ in range(2)]
            await self._wait_entries(2)
            self.assertEqual(main._write_sem._value, 0)  # both permits held
            saved = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
        self.assertEqual(saved.get("result"), {"saved": True}, saved)
        self._release.set()
        await asyncio.gather(*writes, return_exceptions=True)


class ExclusiveHoldTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_rebuild_hold_takes_every_save_permit(self):
        with patch.object(main, "_save_sem", asyncio.Semaphore(2)), \
                patch.object(main, "_write_sem", asyncio.Semaphore(2)), \
                patch.object(main, "_read_sem", asyncio.Semaphore(4)), \
                patch.object(main, "_mine_sem", asyncio.Semaphore(1)), \
                patch.object(main, "PALACE_MCP_SAVE_MAX_INFLIGHT", 2):
            async with main._exclusive_palace():
                self.assertEqual(main._save_sem._value, 0)
            self.assertEqual(main._save_sem._value, 2)


class ExclusiveHoldWaitsForRunningSaves(_LaneHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # Production shape: the hold takes as many permits as the lane has.
        self._more = [
            patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.2),
            patch.object(main, "PALACE_MCP_SAVE_MAX_INFLIGHT", 2),
            patch.object(main, "_mine_sem", asyncio.Semaphore(1)),
        ]
        for p in self._more:
            p.start()

    async def asyncTearDown(self):
        # Stop in reverse of start: these were patched over the harness's.
        for p in reversed(self._more):
            p.stop()
        await super().asyncTearDown()

    async def _timed_out_save(self):
        self._install(park_saves=True)
        r = await asyncio.wait_for(main._call(_req("mempalace_diary_write")), timeout=5)
        self.assertEqual(r["error"]["code"], -32001)
        await self._wait_entries(1)
        self.assertEqual(main._save_sem._value, 2)  # its permit is back...
        self.assertEqual(main._save_inflight, 1)  # ...but its thread still runs

    async def test_the_hold_enters_only_after_the_running_save_finishes(self):
        await self._timed_out_save()
        entered = asyncio.Event()

        held_permits = []

        async def _hold():
            async with main._exclusive_palace():
                held_permits.append(main._save_sem._value)
                entered.set()

        task = self._spawn(_hold())
        await asyncio.sleep(0.3)
        self.assertFalse(entered.is_set(), "the rebuild hold raced a running save")
        self._release.set()
        await asyncio.wait_for(entered.wait(), timeout=5)
        await task
        self.assertEqual(held_permits, [0], "a new save could enter during the hold")

    async def test_a_save_that_never_finishes_fails_the_hold_and_frees_its_permits(self):
        await self._timed_out_save()
        with patch.object(main, "PALACE_EXCLUSIVE_SAVE_DRAIN_SECONDS", 0.2):
            with self.assertRaises(RuntimeError):
                async with main._exclusive_palace():
                    pass
        self.assertEqual(main._save_sem._value, 2)
        self.assertEqual(main._write_sem._value, 2)


class SubmitFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_submit_gives_the_save_slot_back(self):
        main._save_inflight = 0
        with patch.object(main, "_save_sem", asyncio.Semaphore(2)), \
                patch.object(main._SAVE_EXECUTOR, "submit", side_effect=RuntimeError("shut down")):
            with self.assertRaises(RuntimeError):
                await main._call(_req("mempalace_diary_write"))
        self.assertEqual(main._save_inflight, 0)


if __name__ == "__main__":
    unittest.main()
