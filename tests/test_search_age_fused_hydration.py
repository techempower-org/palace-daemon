"""Tests for /search/age-fused graph-only hit hydration (palace-daemon#150).

Pre-#150 the handler built graph-only hits as ``{"id": did, "document": None, ...}``
which gave bench consumers ~5.5× narrower context per question than
/search default. Post-#150 graph-only drawers are hydrated from postgres
in a single query so the response shape matches /search.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_search_age_fused_hydration.py -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, params=None):
        # Capture the params so tests can assert on the ids passed in.
        self.last_sql = sql
        self.last_params = params

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def cursor(self):
        return self._cursor

    def close(self):
        pass


class TestAgeFusedHydration(unittest.IsolatedAsyncioTestCase):
    """Graph-only hits must carry text + metadata after #150."""

    def _make_request(self, body: dict):
        class _R:
            async def json(_self_inner):
                return body
        return _R()

    async def _call_endpoint(self, body):
        # Post-#179 Option C: search_age_fused takes a SearchAgeFusedBody
        # instead of a raw Request. Construct one from the dict; pydantic
        # validators run wing-canonicalize + room-validate at this point.
        from search_models import SearchAgeFusedBody
        parsed = SearchAgeFusedBody(**body)
        resp = await main.search_age_fused(parsed, x_api_key=None)
        # FastAPI handlers return dicts directly; JSONResponse only on errors.
        if isinstance(resp, dict):
            return resp
        return json.loads(resp.body)

    def _fake_drawer_row(self, drawer_id, text):
        from datetime import datetime, timezone
        return (
            drawer_id, text, "wing_alpha", "planning",
            "topic-x", "/src/file.md", datetime(2026, 5, 28, tzinfo=timezone.utc),
        )

    async def test_graph_only_hits_get_hydrated(self):
        """A graph-only drawer should have text + wing + room populated."""
        # Vector finds zero hits; graph finds one drawer.
        async def fake_call(req):
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps({"results": []})}]
            }}

        cur = _FakeCursor([self._fake_drawer_row("graph-1", "the full drawer body text")])
        conn = _FakeConn(cur)
        fake_kg = MagicMock()
        # _run_cypher returns a list of (drawer_id, edge_props) tuples
        # (post-#157 — was (drawer_id, count) before the AGE syntax fix).
        # Wrap them in MagicMocks that the daemon's _unwrap_agtype handles.
        fake_kg._run_cypher.return_value = [("graph-1", {"count": 3})]
        fake_kg._unwrap_agtype.side_effect = lambda x: x

        # Pretend the extractor returns one entity per query.
        class _E:
            def __init__(self, name): self.name = name

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=fake_call), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch("psycopg2.connect", return_value=conn), \
             patch.object(main, "_load_age_extractor", return_value=lambda q: [_E("topic-x")]), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=fake_kg), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch.object(main._rerank, "rerank_response", side_effect=lambda q, r, enabled=None: r):
            response = await self._call_endpoint({"query": "test query", "limit": 3})

        results = response["results"]
        self.assertEqual(len(results), 1, "should have 1 graph-only hit")
        hit = results[0]
        # The fix's contract: hydrated graph-only hits expose text + wing + room.
        self.assertEqual(hit["matched_via"], "graph")
        self.assertEqual(hit["text"], "the full drawer body text")
        self.assertEqual(hit["wing"], "wing_alpha")
        self.assertEqual(hit["room"], "planning")
        self.assertEqual(hit["drawer_id"], "graph-1")
        # Pre-#150 behavior: document=None, no text. Post-#150: text populated.
        self.assertNotIn("document", hit)

    async def test_hydration_failure_falls_back_to_minimal_stub(self):
        """If postgres hydration raises, the response stays valid with the historic shape."""
        async def fake_call(req):
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps({"results": []})}]
            }}

        fake_kg = MagicMock()
        fake_kg._run_cypher.return_value = [("graph-1", {"count": 1})]
        fake_kg._unwrap_agtype.side_effect = lambda x: x

        class _E:
            def __init__(self, name): self.name = name

        # Hydration psycopg2.connect raises — fallback path takes over.
        import psycopg2
        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=fake_call), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch("psycopg2.connect", side_effect=psycopg2.OperationalError("postgres down")), \
             patch.object(main, "_load_age_extractor", return_value=lambda q: [_E("topic-x")]), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=fake_kg), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch.object(main._rerank, "rerank_response", side_effect=lambda q, r, enabled=None: r):
            response = await self._call_endpoint({"query": "test query", "limit": 3})

        results = response["results"]
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit["matched_via"], "graph")
        # Fallback shape preserved for resilience.
        self.assertEqual(hit["id"], "graph-1")
        self.assertIsNone(hit["document"])

    async def test_vector_hits_unchanged(self):
        """Drawers found by vector still come through with their full hit dict."""
        vec_hit = {
            "drawer_id": "vec-1", "text": "vector body",
            "wing": "wing_beta", "room": "sessions",
            "topic": "alpha", "source_file": "f.py",
            "created_at": "2026-05-28", "similarity": 0.92,
        }

        async def fake_call(req):
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": json.dumps({"results": [vec_hit]})}]
            }}

        fake_kg = MagicMock()
        fake_kg._run_cypher.return_value = []
        fake_kg._unwrap_agtype.side_effect = lambda x: x

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=fake_call), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main, "_load_age_extractor", return_value=lambda q: []), \
             patch("mempalace.knowledge_graph_age.KnowledgeGraphAGE", return_value=fake_kg), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch.object(main._rerank, "rerank_response", side_effect=lambda q, r, enabled=None: r):
            response = await self._call_endpoint({"query": "vector only", "limit": 3})

        results = response["results"]
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit["matched_via"], "vector")
        self.assertEqual(hit["text"], "vector body")
        # vec-hits keep their original drawer_id form.
        self.assertEqual(hit["drawer_id"], "vec-1")


if __name__ == "__main__":
    unittest.main()
