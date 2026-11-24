"""/search/hybrid stage timings and the budget_ms degrade (palace-daemon#284).

Under a mine drain the hybrid route took 24 s while /search/fast answered in
30 ms, so the pre-mutation hook (2 s ceiling) timed out exactly when it was
needed. The route now reports ``timings_ms`` on every answer, and a caller
that names ``budget_ms`` gets the BM25 answer, marked ``degraded``, instead of
waiting past its budget or failing on a busy refusal.

Run with::

    venv/bin/python -m pytest tests/test_search_hybrid_budget.py -q
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
import unittest
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from search_models import SearchHybridBody  # noqa: E402

_HYBRID_OK = {"jsonrpc": "2.0", "id": 1, "result": {
    "content": [{"type": "text", "text": '{"results":[{"id":"v1","text":"vector hit"}]}'}]
}}

_FAST_HITS = [
    {"id": "f1", "wing": "w", "room": "problems", "rank": 0.9, "snippet": "pjsip one"},
    {"id": "f2", "wing": "w", "room": "planning", "rank": 0.8, "snippet": "pjsip two"},
    {"id": "f3", "wing": "w", "room": "problems", "rank": 0.7, "snippet": "pjsip three"},
]


class TestSearchHybridBudget(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fake_config = type("FakeConfig", (), {"backend": "postgres", "palace_path": "/srv/test"})()
        self._patches = [
            patch.object(main._mp, "_config", fake_config),
            patch.object(main, "_canonical_rooms", return_value={"problems", "planning"}),
            patch.object(main, "_check_auth"),
            patch.object(main._rerank, "rerank_response",
                         side_effect=lambda q, r, enabled=None: r),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    async def _run(self, body, call, fast=None):
        fast_mock = AsyncMock(return_value=list(_FAST_HITS) if fast is None else fast)
        with patch.object(main, "_call", side_effect=call), \
                patch.object(main, "search_fast", fast_mock):
            out = await main.search_hybrid(SearchHybridBody(**body), x_api_key="k")
        return out, fast_mock

    async def test_no_budget_returns_hybrid_with_timings(self):
        async def call(_env, **_kw):
            return _HYBRID_OK
        out, fast = await self._run({"query": "pjsip"}, call)
        self.assertNotIn("degraded", out)
        self.assertEqual(out["results"][0]["id"], "v1")
        self.assertEqual(set(out["timings_ms"]), {"hybrid", "rerank", "total"})
        fast.assert_not_awaited()

    async def test_within_budget_returns_hybrid(self):
        async def call(_env, **_kw):
            return _HYBRID_OK
        out, fast = await self._run({"query": "pjsip", "budget_ms": 2000}, call)
        self.assertNotIn("degraded", out)
        fast.assert_not_awaited()

    async def test_over_budget_degrades_to_fast_with_marker(self):
        async def call(_env, **_kw):
            await asyncio.sleep(5)
            return _HYBRID_OK
        out, fast = await self._run({"query": "pjsip", "limit": 2, "budget_ms": 50}, call)
        self.assertEqual(out["degraded"], "bm25-fast")
        self.assertEqual([h["id"] for h in out["results"]], ["f1", "f2"])
        self.assertEqual(out["results"][0]["text"], "pjsip one")
        self.assertEqual(out["results"][0]["matched_via"], "bm25_postgres")
        self.assertIn("budget_ms=50", out["warnings"][0])
        self.assertEqual(set(out["timings_ms"]), {"hybrid", "fast", "total"})
        self.assertLess(out["timings_ms"]["total"], 2000)
        fast.assert_awaited_once()

    async def test_busy_refusal_degrades(self):
        async def call(_env, **_kw):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32003, "message": "daemon busy"}}
        out, _ = await self._run({"query": "pjsip", "budget_ms": 2000}, call)
        self.assertEqual(out["degraded"], "bm25-fast")
        self.assertIn("-32003", out["warnings"][0])

    async def test_busy_refusal_without_budget_is_unchanged(self):
        busy = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "daemon busy"}}

        async def call(_env, **_kw):
            return busy
        out, fast = await self._run({"query": "pjsip"}, call)
        self.assertNotIn("degraded", out)
        self.assertEqual(out["error"]["code"], -32003)
        fast.assert_not_awaited()

    async def test_room_filter_applied_to_fallback(self):
        async def call(_env, **_kw):
            await asyncio.sleep(5)
            return _HYBRID_OK
        out, fast = await self._run(
            {"query": "pjsip", "room": "problems", "limit": 5, "budget_ms": 50}, call)
        self.assertEqual([h["id"] for h in out["results"]], ["f1", "f3"])
        self.assertEqual(fast.await_args.kwargs["limit"], 15)

    async def test_fallback_failure_is_504(self):
        async def call(_env, **_kw):
            await asyncio.sleep(5)
            return _HYBRID_OK
        fast_mock = AsyncMock(side_effect=HTTPException(status_code=500, detail="db down"))
        with patch.object(main, "_call", side_effect=call), \
                patch.object(main, "search_fast", fast_mock):
            with self.assertRaises(HTTPException) as ctx:
                await main.search_hybrid(
                    SearchHybridBody(query="pjsip", budget_ms=50), x_api_key="k")
        self.assertEqual(ctx.exception.status_code, 504)
        self.assertIn("db down", ctx.exception.detail)

    def test_budget_bounds_validated(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            SearchHybridBody(query="q", budget_ms=0)

    async def test_timings_split_queue_from_compute(self):
        # _call stamps the box: entered before the read semaphore, started
        # when the worker thread began. The route turns those into queue
        # (waited) and compute (ran) so a slow search says which it was.
        async def call(_env, box=None, **_kw):
            now = time.monotonic()
            box["entered"] = now - 0.5
            box["started"] = now - 0.1
            return _HYBRID_OK
        out, _ = await self._run({"query": "pjsip"}, call)
        t = out["timings_ms"]
        self.assertEqual(set(t), {"hybrid", "queue", "compute", "rerank", "total"})
        self.assertGreaterEqual(t["queue"], 390)
        self.assertLess(t["queue"], 450)
        self.assertGreaterEqual(t["compute"], 90)

    async def test_over_budget_reports_queue_only_when_worker_never_started(self):
        async def call(_env, box=None, **_kw):
            box["entered"] = time.monotonic()
            await asyncio.sleep(5)
            return _HYBRID_OK
        out, _ = await self._run({"query": "pjsip", "budget_ms": 50}, call)
        self.assertEqual(out["degraded"], "bm25-fast")
        self.assertEqual(set(out["timings_ms"]), {"hybrid", "queue", "compute", "fast", "total"})
        self.assertEqual(out["timings_ms"]["compute"], 0.0)
        self.assertGreaterEqual(out["timings_ms"]["queue"], 45)

    async def test_budget_expiry_abandons_the_call_without_cancelling_it(self):
        # A _call whose worker is already submitted keeps running to its own
        # end (and so keeps its read-semaphore slot) rather than being
        # cancelled by the budget.
        seen = {}

        async def call(_env, box=None, **_kw):
            box["entered"] = box["submitted"] = time.monotonic()
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                seen["cancelled"] = True
                raise
            seen["finished"] = True
            return _HYBRID_OK
        out, _ = await self._run({"query": "pjsip", "budget_ms": 50}, call)
        self.assertEqual(out["degraded"], "bm25-fast")
        await asyncio.sleep(0.3)
        self.assertEqual(seen, {"finished": True})


class TestBudgetedCallsRespectReadConcurrency(unittest.IsolatedAsyncioTestCase):
    """Through the real main._call: budgeted callers that give up must not
    free the read semaphore while their worker thread runs on. N budgeted
    calls never run more than PALACE_MAX_READ_CONCURRENCY handlers at once,
    and a following call with no budget queues instead of getting -32003.
    A budgeted call still queued for a slot when its budget ends is
    cancelled instead: it holds nothing, and nobody will read its answer."""

    def setUp(self):
        fake_config = type("FakeConfig", (), {"backend": "postgres", "palace_path": "/srv/test"})()
        self._patches = [
            patch.object(main._mp, "_config", fake_config),
            patch.object(main, "_canonical_rooms", return_value={"problems", "planning"}),
            patch.object(main, "_check_auth"),
            patch.object(main._rerank, "rerank_response",
                         side_effect=lambda q, r, enabled=None: r),
            patch.object(main, "_read_sem", asyncio.Semaphore(1)),
            patch.object(main, "_tool_inflight", 0),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()

    async def _drain(self):
        me = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not me]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for _ in range(20):  # let the done-callbacks hop back to the loop
            await asyncio.sleep(0.01)

    async def test_abandoned_calls_hold_their_slot_and_later_callers_queue(self):
        lock = threading.Lock()
        running = {"now": 0, "peak": 0, "calls": 0}

        def handle_request(_req):
            with lock:
                running["now"] += 1
                running["calls"] += 1
                running["peak"] = max(running["peak"], running["now"])
            try:
                time.sleep(0.25)
                return _HYBRID_OK
            finally:
                with lock:
                    running["now"] -= 1

        fast_mock = AsyncMock(return_value=list(_FAST_HITS))
        with patch.object(main._mp, "handle_request", handle_request), \
                patch.object(main, "search_fast", fast_mock):
            budgeted = [
                main.search_hybrid(SearchHybridBody(query="pjsip", budget_ms=50), x_api_key="k")
                for _ in range(3)
            ]
            outs = await asyncio.gather(*budgeted)
            self.assertEqual([o["degraded"] for o in outs], ["bm25-fast"] * 3)
            # One worker holds the read slot and runs on; the two calls that
            # were still queued for it are cancelled. A caller with no budget
            # queues behind the running one and gets a real answer, never a
            # -32003 refusal.
            out = await main.search_hybrid(SearchHybridBody(query="pjsip"), x_api_key="k")
            self.assertNotIn("degraded", out)
            self.assertNotIn("error", out)
            self.assertEqual(out["results"][0]["id"], "v1")
            await self._drain()
        self.assertEqual(running["peak"], 1)
        self.assertEqual(running["calls"], 2)
        self.assertEqual(main._tool_inflight, 0)

    async def test_budget_expiry_cancels_a_call_still_queued_for_a_slot(self):
        calls = []

        def handle_request(_req):
            calls.append(1)
            return _HYBRID_OK

        fast_mock = AsyncMock(return_value=list(_FAST_HITS))
        with patch.object(main._mp, "handle_request", handle_request), \
                patch.object(main, "search_fast", fast_mock):
            await main._read_sem.acquire()  # the one read slot is taken elsewhere
            try:
                out = await main.search_hybrid(
                    SearchHybridBody(query="pjsip", budget_ms=50), x_api_key="k")
                self.assertEqual(out["degraded"], "bm25-fast")
                self.assertEqual(out["timings_ms"]["compute"], 0.0)
            finally:
                main._read_sem.release()
            await self._drain()  # inside the patch: a surviving task would hit the fake
        # The queued search never ran: its slot went to nobody and no thread
        # was spent on an answer with no reader.
        self.assertEqual(calls, [])
        self.assertEqual(main._tool_inflight, 0)


if __name__ == "__main__":
    unittest.main()
