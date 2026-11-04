"""A slow-path worker that outlives its caller's timeout logs where it is stuck (#323).

Every postgres statement is bounded by the palace role's statement_timeout,
so a tool call still running after its caller timed out is waiting on
something else: a lock, a pool checkout, a long loop. On 2026-11-04 the
daemon log could not say which. The stack names it for the next hang.

Run with::

    python -m pytest tests/test_overdue_stack.py -q
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
    return {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": tool, "arguments": args or {"query": "SECRET-QUERY-TEXT"}}}


class OverdueStackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.release = threading.Event()
        main._tool_inflight = 0
        self._p = [
            patch.object(main, "PALACE_MCP_TOOL_TIMEOUT_SECONDS", 0.2),
            patch.object(main, "_read_sem", asyncio.Semaphore(4)),
        ]
        for p in self._p:
            p.start()

    async def asyncTearDown(self):
        self.release.set()
        for _ in range(100):
            if main._tool_inflight == 0:
                break
            await asyncio.sleep(0.02)
        for p in self._p:
            p.stop()
        main._tool_inflight = 0

    def _waits_on_the_kg_lock(self, request_dict):
        self.release.wait(timeout=10)
        return {"jsonrpc": "2.0", "id": 4, "result": {}}

    async def test_an_overdue_call_logs_its_worker_stack_once(self):
        with patch.object(main._mp, "handle_request", side_effect=self._waits_on_the_kg_lock), \
                self.assertLogs(main._log, level=logging.WARNING) as cap:
            env = await asyncio.wait_for(main._call(_req()), 5)
        self.assertEqual(env["error"]["code"], -32001)
        overdue = [r for r in cap.output if "slow path overdue" in r]
        self.assertEqual(len(overdue), 1, cap.output)
        self.assertIn("tool=mempalace_search", overdue[0])
        self.assertIn("_waits_on_the_kg_lock", overdue[0])
        self.assertNotIn("SECRET-QUERY-TEXT", "\n".join(cap.output))

    async def test_a_call_that_finishes_in_time_logs_no_stack(self):
        ok = {"jsonrpc": "2.0", "id": 4, "result": {}}
        with patch.object(main._mp, "handle_request", return_value=ok), \
                patch.object(main._log, "warning") as warn:
            env = await asyncio.wait_for(main._call(_req()), 5)
        self.assertNotIn("error", env)
        self.assertFalse(any("overdue" in str(c) for c in warn.call_args_list))

    async def test_a_failing_instrument_does_not_change_the_answer(self):
        with patch.object(main._mp, "handle_request", side_effect=self._waits_on_the_kg_lock), \
                patch.object(main, "_log_overdue_stack", side_effect=RuntimeError("boom")):
            env = await asyncio.wait_for(main._call(_req()), 5)
        self.assertEqual(env["error"]["code"], -32001)

    def test_no_stack_when_the_worker_never_started_or_finished(self):
        with patch.object(main._log, "warning") as warn:
            main._log_overdue_stack("t", {}, {})
            main._log_overdue_stack("t", {}, {"ident": -1})
        warn.assert_not_called()

    def test_a_finished_call_on_a_still_busy_thread_logs_nothing(self):
        # The executor reuses threads: after call A finishes, the same thread
        # runs call B. A's box must not report B's stack.
        import concurrent.futures as cf

        gate = threading.Event()
        with cf.ThreadPoolExecutor(max_workers=1) as ex:
            box_a, box_b = {}, {}
            ex.submit(main._run_tracked, lambda r: None, {}, box_a).result()
            fut_b = ex.submit(main._run_tracked, lambda r: gate.wait(5), {}, box_b)
            for _ in range(100):
                if box_b.get("ident"):
                    break
                threading.Event().wait(0.01)
            self.assertEqual(box_a["ident"], box_b["ident"])  # same thread
            with patch.object(main._log, "warning") as warn:
                main._log_overdue_stack("a", {}, box_a)  # finished
                main._log_overdue_stack("a", {}, dict(box_b, done=False) | {"x": 1})  # foreign box
            warn.assert_not_called()
            with patch.object(main._log, "warning") as warn:
                main._log_overdue_stack("b", {}, box_b)  # its own, still running
            self.assertEqual(warn.call_count, 1)
            gate.set()
            fut_b.result()

    async def test_no_source_text_reaches_the_log(self):
        marker = "SOURCE-LINE-MARKER"

        def _stuck(request_dict):
            self.release.wait(timeout=10)  # SOURCE-LINE-MARKER
            return {"jsonrpc": "2.0", "id": 4, "result": {}}

        with patch.object(main._mp, "handle_request", side_effect=_stuck), \
                self.assertLogs(main._log, level=logging.WARNING) as cap:
            await asyncio.wait_for(main._call(_req()), 5)
        text = "\n".join(cap.output)
        self.assertIn("in _stuck", text)
        self.assertNotIn(marker, text)


if __name__ == "__main__":
    unittest.main()
