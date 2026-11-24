"""Judged drawer classes on the daemon's search routes (mempalace docs/designs/judged-drawer-classes.md).

A drawer flagged ``judged_class`` chatter or stale is left out of default search; ``all_classes`` brings it
back, so a flagged drawer is never unreachable. Routes that go through the ``mempalace_search`` tool pass the
flag on (mempalace filters there); routes with their own SQL (/search/fast, /search/keyword, the graph-only
hits of /search/age-fused) filter here.

Run with::

    venv/bin/python -m pytest tests/test_search_judged_classes.py -q
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import search_routes  # noqa: E402
from search_models import SearchHybridBody, SearchKeywordBody  # noqa: E402


def _envelope(payload):
    return {"result": {"content": [{"text": json.dumps(payload)}]}}


class _Cursor:
    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, [], []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, params=None):
        self.sql.append(sql)
        self.params.append(params)

    def fetchall(self):
        return self.rows


class _Conn:
    def __init__(self, cur):
        self.cur = cur

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self):
        return self.cur

    def close(self):
        pass


class ToolRoutesPassTheFlag(unittest.IsolatedAsyncioTestCase):
    async def _search(self, **kw):
        call = AsyncMock(return_value=_envelope({"results": []}))
        with patch.object(main, "_check_auth"), patch.object(main, "_call", call):
            await search_routes.search(q="q", limit=5, kind="all", wing=None, room=None, rerank=False, **kw)
        return call.call_args.args[0]["params"]["arguments"]

    async def test_search_default_does_not_send_the_flag(self):
        self.assertNotIn("all_classes", await self._search(all_classes=False))

    async def test_search_all_classes_reaches_the_tool(self):
        self.assertIs((await self._search(all_classes=True))["all_classes"], True)

    async def test_context_all_classes_reaches_the_tool(self):
        call = AsyncMock(return_value=_envelope({"results": []}))
        with patch.object(main, "_check_auth"), patch.object(main, "_call", call):
            await search_routes.context(topic="t", limit=5, kind="all", all_classes=True)
        self.assertIs(call.call_args.args[0]["params"]["arguments"]["all_classes"], True)

    async def test_hybrid_body_flag_reaches_the_tool(self):
        call = AsyncMock(return_value=_envelope({"results": []}))
        body = SearchHybridBody(query="q", all_classes=True, rerank=False)
        with patch.object(main, "_check_auth"), patch.object(main, "_call", call), \
             patch.object(main._mp, "_config", type("FakeConfig", (), {"backend": "postgres", "palace_path": "/nonexistent"})()):
            await search_routes.search_hybrid(body)
        self.assertIs(call.call_args.args[0]["params"]["arguments"]["all_classes"], True)


class FastRouteFiltersInSql(unittest.IsolatedAsyncioTestCase):
    async def _sql(self, all_classes):
        cur = _Cursor([])
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("psycopg2.connect", return_value=_Conn(cur)):
            await main.search_fast(q="q", limit=5, wing=None, all_classes=all_classes, x_api_key=None)
        return cur.sql[-1]

    async def test_default_excludes_chatter_and_stale(self):
        sql = await self._sql(False)
        self.assertIn("judged_class", sql)
        self.assertIn("NOT IN ('chatter', 'stale')", sql)
        # NULL (unflagged) must pass: COALESCE keeps every unflagged drawer
        self.assertIn("COALESCE(metadata->>'judged_class', '')", sql)

    async def test_all_classes_has_no_judged_predicate(self):
        self.assertNotIn("judged_class", await self._sql(True))


class DropJudgedHelper(unittest.TestCase):
    def test_drops_flagged_counts_and_trims(self):
        resp = {"results": [{"drawer_id": "a"}, {"id": "b"}, {"drawer_id": "c"}, {"drawer_id": "d"}]}
        with patch.object(search_routes, "_judged_hidden_ids", return_value={"a", "b"}):
            out = search_routes._drop_judged(resp, "dsn", limit=1)
        self.assertEqual(out["results"], [{"drawer_id": "c"}])
        self.assertEqual(out["judged_excluded"], 2)

    def test_lookup_fails_open(self):
        with patch("psycopg2.connect", side_effect=RuntimeError("db down")):
            self.assertEqual(search_routes._judged_hidden_ids(["a"], "dsn"), set())

    def test_lookup_reads_only_the_excluded_classes(self):
        cur = _Cursor([("a",)])
        with patch("psycopg2.connect", return_value=_Conn(cur)):
            self.assertEqual(search_routes._judged_hidden_ids(["a", "b"], "dsn"), {"a"})
        self.assertEqual(cur.params[-1][1], ["chatter", "stale"])


class KeywordRouteFilters(unittest.IsolatedAsyncioTestCase):
    async def _run(self, all_classes, hidden):
        bm25 = {"results": [{"drawer_id": "a"}, {"drawer_id": "b"}, {"drawer_id": "c"}]}
        body = SearchKeywordBody(query="q", limit=2, all_classes=all_classes, rerank=False)
        with patch.object(main, "_check_auth"), \
             patch.object(main._mp, "_config", type("FakeConfig", (), {"backend": "postgres"})()), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("mempalace.searcher._bm25_only_via_postgres",
                   side_effect=lambda *a, **k: json.loads(json.dumps(bm25))) as fetch, \
             patch.object(search_routes, "_judged_hidden_ids", return_value=hidden):
            out = await search_routes.search_keyword(body)
        return out, [c.kwargs["n_results"] for c in fetch.call_args_list]

    async def test_unflagged_request_is_unchanged(self):
        out, fetched = await self._run(False, set())
        self.assertEqual(fetched, [2])
        self.assertEqual([h["drawer_id"] for h in out["results"]], ["a", "b", "c"])
        self.assertNotIn("judged_excluded", out)

    async def test_flagged_hits_refetch_wider_and_filter(self):
        out, fetched = await self._run(False, {"a"})
        self.assertEqual(fetched, [2, 4])
        self.assertEqual([h["drawer_id"] for h in out["results"]], ["b", "c"])
        self.assertEqual(out["judged_excluded"], 1)
        self.assertTrue(any("all_classes" in w for w in out["warnings"]))

    async def test_a_stalled_lookup_does_not_block_the_event_loop(self):
        import asyncio
        import time

        def slow_lookup(ids, dsn):
            time.sleep(0.3)  # a blocking psycopg2 call that hangs
            return set()

        gaps = []

        async def ticker():
            last = time.monotonic()
            end = last + 0.6
            while time.monotonic() < end:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        bm25 = {"results": [{"drawer_id": "a"}]}
        body = SearchKeywordBody(query="q", limit=2, rerank=False)
        with patch.object(main, "_check_auth"), \
             patch.object(main._mp, "_config", type("FakeConfig", (), {"backend": "postgres"})()), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("mempalace.searcher._bm25_only_via_postgres", return_value=bm25), \
             patch.object(search_routes, "_judged_hidden_ids", side_effect=slow_lookup):
            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)  # the ticker is running before the request starts
            await search_routes.search_keyword(body)
            await tick
        self.assertLess(max(gaps), 0.15, "the event loop stalled while the lookup ran")

    async def test_a_stalled_wider_fetch_does_not_block_the_event_loop(self):
        """xreview r3: the refetch after a flagged hit is a blocking DB call too."""
        import asyncio
        import time

        calls = []

        def bm25(*a, **k):
            calls.append(k["n_results"])
            if len(calls) > 1:
                time.sleep(0.3)  # the wider fetch hangs
            return {"results": [{"drawer_id": "a"}, {"drawer_id": "b"}]}

        gaps = []

        async def ticker():
            last = time.monotonic()
            end = last + 0.6
            while time.monotonic() < end:
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        body = SearchKeywordBody(query="q", limit=2, rerank=False)
        with patch.object(main, "_check_auth"), \
             patch.object(main._mp, "_config", type("FakeConfig", (), {"backend": "postgres"})()), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("mempalace.searcher._bm25_only_via_postgres", side_effect=bm25), \
             patch.object(search_routes, "_judged_hidden_ids", return_value={"a"}):
            tick = asyncio.create_task(ticker())
            await asyncio.sleep(0.05)
            await search_routes.search_keyword(body)
            await tick
        self.assertEqual(len(calls), 2, "the flagged hit must trigger the wider fetch")
        self.assertLess(max(gaps), 0.15, "the event loop stalled during the wider fetch")

    async def _run_wider(self, wider_page):
        first = {"results": [{"drawer_id": "a"}, {"drawer_id": "b"}]}
        pages = [first, wider_page]
        body = SearchKeywordBody(query="q", limit=2, rerank=False)
        with patch.object(main, "_check_auth"), \
             patch.object(main._mp, "_config", type("FakeConfig", (), {"backend": "postgres"})()), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("mempalace.searcher._bm25_only_via_postgres", side_effect=lambda *a, **k: pages.pop(0)), \
             patch.object(search_routes, "_judged_hidden_ids", return_value={"a"}):
            return await search_routes.search_keyword(body)

    async def test_failed_wider_fetch_keeps_the_first_page(self):
        """xreview r4: an error or empty refetch must not throw away the hits we had."""
        for wider in ({"error": "db timeout"}, {"results": []}):
            out = await self._run_wider(wider)
            self.assertEqual([h["drawer_id"] for h in out["results"]], ["b"], wider)
            self.assertEqual(out["judged_excluded"], 1, wider)

    async def test_all_classes_returns_everything(self):
        out, fetched = await self._run(True, {"a"})
        self.assertEqual(fetched, [2])
        self.assertEqual([h["drawer_id"] for h in out["results"]], ["a", "b", "c"])


class AgeFusedGraphOnlyHits(unittest.IsolatedAsyncioTestCase):
    """Graph-only hits are hydrated by the daemon's own SQL, so it filters them (vector hits: mempalace)."""

    async def _run(self, judged_class, all_classes, vec_payload=None):
        from datetime import datetime, timezone
        from unittest.mock import MagicMock

        from search_models import SearchAgeFusedBody

        async def fake_call(req, **_kw):
            return _envelope(vec_payload or {"results": []})

        row = ("graph-1", "body", "wing_alpha", "planning", "topic-x", "/src/f.md",
               datetime(2026, 5, 28, tzinfo=timezone.utc), judged_class)
        fake_kg = MagicMock()
        fake_kg._run_cypher.return_value = [("graph-1", {"count": 3})]
        fake_kg._unwrap_agtype.side_effect = lambda x: x

        class _E:
            def __init__(self, name):
                self.name = name

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=fake_call), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch("psycopg2.connect", return_value=_Conn(_Cursor([row]))), \
             patch.object(main, "_load_age_extractor", return_value=lambda q: [_E("topic-x")]), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=fake_kg), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch.object(main._rerank, "rerank_response", side_effect=lambda q, r, enabled=None: r):
            return await main.search_age_fused(
                SearchAgeFusedBody(query="q", limit=3, all_classes=all_classes), x_api_key=None
            )

    async def test_flagged_graph_only_hit_is_left_out(self):
        out = await self._run("chatter", all_classes=False)
        self.assertEqual(out["results"], [])
        self.assertEqual(out["judged_excluded"], 1)

    async def test_all_classes_keeps_it(self):
        out = await self._run("chatter", all_classes=True)
        self.assertEqual([h["drawer_id"] for h in out["results"]], ["graph-1"])

    async def test_vector_step_count_and_warning_are_carried(self):
        payload = {"results": [], "judged_excluded": 2, "warnings": ["2 hit(s) judged chatter/stale were left out"]}
        out = await self._run(None, all_classes=False, vec_payload=payload)
        self.assertEqual(out["judged_excluded"], 2)
        self.assertIn("2 hit(s) judged chatter/stale were left out", out["warnings"])

    async def test_secret_and_unflagged_are_not_excluded(self):
        for cls in ("secret", None):
            out = await self._run(cls, all_classes=False)
            self.assertEqual([h["drawer_id"] for h in out["results"]], ["graph-1"], cls)


if __name__ == "__main__":
    unittest.main()
