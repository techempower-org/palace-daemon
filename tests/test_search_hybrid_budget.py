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
        async def call(_env):
            return _HYBRID_OK
        out, fast = await self._run({"query": "pjsip"}, call)
        self.assertNotIn("degraded", out)
        self.assertEqual(out["results"][0]["id"], "v1")
        self.assertEqual(set(out["timings_ms"]), {"hybrid", "rerank", "total"})
        fast.assert_not_awaited()

    async def test_within_budget_returns_hybrid(self):
        async def call(_env):
            return _HYBRID_OK
        out, fast = await self._run({"query": "pjsip", "budget_ms": 2000}, call)
        self.assertNotIn("degraded", out)
        fast.assert_not_awaited()

    async def test_over_budget_degrades_to_fast_with_marker(self):
        async def call(_env):
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
        async def call(_env):
            return {"jsonrpc": "2.0", "id": 1,
                    "error": {"code": -32003, "message": "daemon busy"}}
        out, _ = await self._run({"query": "pjsip", "budget_ms": 2000}, call)
        self.assertEqual(out["degraded"], "bm25-fast")
        self.assertIn("-32003", out["warnings"][0])

    async def test_busy_refusal_without_budget_is_unchanged(self):
        busy = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32003, "message": "daemon busy"}}

        async def call(_env):
            return busy
        out, fast = await self._run({"query": "pjsip"}, call)
        self.assertNotIn("degraded", out)
        self.assertEqual(out["error"]["code"], -32003)
        fast.assert_not_awaited()

    async def test_room_filter_applied_to_fallback(self):
        async def call(_env):
            await asyncio.sleep(5)
            return _HYBRID_OK
        out, fast = await self._run(
            {"query": "pjsip", "room": "problems", "limit": 5, "budget_ms": 50}, call)
        self.assertEqual([h["id"] for h in out["results"]], ["f1", "f3"])
        self.assertEqual(fast.await_args.kwargs["limit"], 15)

    async def test_fallback_failure_is_504(self):
        async def call(_env):
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


if __name__ == "__main__":
    unittest.main()
