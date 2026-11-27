"""Tests for the /list fast path (#231).

The upstream ``mempalace_list_drawers`` tool fetches every drawer
(documents included) before slicing the page — ~50s for ``limit=5`` at
490K drawers with no wing filter. The fast path serves the same envelope
from direct SQL. Covered here:

- envelope-entry construction (preview truncation, source_file basename,
  wing/room injection, tolerant tags parsing)
- GET /list prefers the fast path and falls back to the MCP tool when it
  raises (#49 pattern)
- /mcp fast-intercepts ``mempalace_list_drawers`` ONLY for the
  wing/room/limit/offset argument subset — ``since``/``before``/``tags``
  fall through to the upstream tool

Run with::

    cd /home/jp/Projects/palace-daemon
    PYTHONPATH=. venv/bin/python -m pytest tests/test_fast_list.py -q
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

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

import fast_intercept  # noqa: E402
import main  # noqa: E402

_FAST_ENVELOPE = {
    "drawers": [{"drawer_id": "d1", "wing": "general", "room": "", "content_preview": "x",
                 "metadata": {"wing": "general", "room": ""}, "tags": []}],
    "total": 1, "count": 1, "offset": 0, "limit": 5,
}


class TestListEntry(unittest.TestCase):
    def test_preview_truncates_at_200(self):
        entry = fast_intercept._list_entry("d1", "a" * 300, "w", "r", {})
        self.assertEqual(entry["content_preview"], "a" * 200 + "...")
        entry = fast_intercept._list_entry("d1", "short", "w", "r", {})
        self.assertEqual(entry["content_preview"], "short")

    def test_wing_room_injected_and_source_file_basenamed(self):
        meta = {"source_file": "/very/long/path/session.jsonl"}
        entry = fast_intercept._list_entry("d1", "doc", "wingx", "roomy", meta)
        self.assertEqual(entry["wing"], "wingx")
        self.assertEqual(entry["metadata"]["wing"], "wingx")
        self.assertEqual(entry["metadata"]["room"], "roomy")
        self.assertEqual(entry["metadata"]["source_file"], "session.jsonl")

    def test_tags_tolerant_parsing(self):
        self.assertEqual(fast_intercept._tags_from_meta({"tags": ["a", "b"]}), ["a", "b"])
        self.assertEqual(fast_intercept._tags_from_meta({"tags": '["a","b"]'}), ["a", "b"])
        self.assertEqual(fast_intercept._tags_from_meta({"tags": "a, b"}), ["a", "b"])
        self.assertEqual(fast_intercept._tags_from_meta({}), [])
        self.assertEqual(fast_intercept._tags_from_meta(None), [])


class TestListRouteFastPath(unittest.IsolatedAsyncioTestCase):
    async def test_route_returns_fast_payload(self):
        with patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_list_payload", return_value=_FAST_ENVELOPE) as fp, \
             patch.object(main, "_call", new_callable=AsyncMock) as slow:
            resp = await main.list_drawers(wing="general", room=None, limit=5, offset=0,
                                           x_api_key=None)
        self.assertEqual(resp, _FAST_ENVELOPE)
        fp.assert_called_once_with(wing="general", room=None, limit=5, offset=0)
        slow.assert_not_awaited()

    async def test_route_503s_on_fast_failure_instead_of_the_full_drawer_tool(self):
        # #323: the tool fetches every drawer before paginating; a fast-path
        # failure must not turn into that fetch.
        from fastapi import HTTPException

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_fast_list_payload", side_effect=RuntimeError("pg down")), \
             patch.object(main, "_call", new_callable=AsyncMock) as slow:
            with self.assertRaises(HTTPException) as cm:
                await main.list_drawers(wing=None, room=None, limit=5, offset=0,
                                        x_api_key=None)
        self.assertEqual(cm.exception.status_code, 503)
        self.assertIn("pg down", cm.exception.detail)
        slow.assert_not_awaited()

    async def test_route_honors_intercept_kill_switch(self):
        tool_payload = {"drawers": [], "total": 0, "count": 0, "offset": 0, "limit": 5}
        slow_envelope = {"jsonrpc": "2.0", "id": 1,
                         "result": {"content": [{"type": "text",
                                                 "text": json.dumps(tool_payload)}]}}
        with patch.object(main, "_check_auth"), \
             patch.object(main, "PALACE_MCP_FAST_INTERCEPT", False), \
             patch.object(main, "_fast_list_payload") as fp, \
             patch.object(main, "_call", new_callable=AsyncMock,
                          return_value=slow_envelope):
            resp = await main.list_drawers(wing=None, room=None, limit=5, offset=0,
                                           x_api_key=None)
        self.assertEqual(resp, tool_payload)
        fp.assert_not_called()


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed in test env")
class TestMcpListIntercept(unittest.TestCase):
    def _post(self, arguments):
        client = TestClient(main.app)
        with patch.object(main, "_check_auth"):
            return client.post("/mcp", json={
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "mempalace_list_drawers", "arguments": arguments},
            })

    def test_intercepts_plain_listing_args(self):
        with patch.object(main, "_fast_list_payload", return_value=_FAST_ENVELOPE) as fp, \
             patch.object(main, "_call", new_callable=AsyncMock) as slow:
            resp = self._post({"wing": "general", "limit": 5})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("result", body)
        payload = json.loads(body["result"]["content"][0]["text"])
        self.assertEqual(payload, _FAST_ENVELOPE)
        fp.assert_called_once_with(wing="general", limit=5)
        slow.assert_not_awaited()

    def test_since_filter_falls_through_to_tool(self):
        slow_envelope = {"jsonrpc": "2.0", "id": 3, "result": {"content": [
            {"type": "text", "text": json.dumps({"drawers": []})}]}}
        with patch.object(main, "_fast_list_payload") as fp, \
             patch.object(main, "_call", new_callable=AsyncMock,
                          return_value=slow_envelope) as slow:
            resp = self._post({"wing": "general", "since": "2026-08-01"})
        self.assertEqual(resp.status_code, 200)
        fp.assert_not_called()
        slow.assert_awaited_once()

    def test_fast_failure_is_an_error_not_the_tool(self):
        with patch.object(main, "_fast_list_payload", side_effect=RuntimeError("pg down")), \
             patch.object(main, "_call", new_callable=AsyncMock) as slow:
            resp = self._post({"limit": 5})
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["error"]["code"], -32004)
        self.assertIn("pg down", body["error"]["message"])
        slow.assert_not_awaited()


class _FakeDrawers:
    """A tiny in-memory mempalace_drawers that answers the fast path's SQL.

    Records which drawer ids had their ``document`` read by any query, and
    every SQL text run. An unknown statement fails the test."""

    def __init__(self, rows):
        self.rows = rows
        self.bodies_read: set = set()
        self.sql: list = []

    @staticmethod
    def _parent(r):
        return r["metadata"].get("parent_drawer_id")

    def _filtered(self, sql, params):
        rows, p = list(self.rows), list(params or [])
        if "parent_drawer_id' IS NULL" in sql:
            rows = [r for r in rows if self._parent(r) is None]
        if "parent_drawer_id' IS NOT NULL" in sql:
            rows = [r for r in rows if self._parent(r) is not None]
        if "wing = %s" in sql:
            w = p.pop(0)
            rows = [r for r in rows if r["wing"] == w]
        if "room = %s" in sql:
            rm = p.pop(0)
            rows = [r for r in rows if r["room"] == rm]
        return rows, p

    def run(self, sql, params):
        self.sql.append(sql)
        if sql.startswith("SET LOCAL"):
            return []
        if sql.startswith("SELECT count(*)"):
            return [(len(self._filtered(sql, params)[0]),)]
        if " AS parent" in sql:
            cols = [c.strip() for c in sql.split("SELECT", 1)[1].split("FROM", 1)[0].split(",")]
            rows, _ = self._filtered(sql, params)
            rows.sort(key=lambda r: (self._parent(r), r["metadata"].get("chunk_index", 0), r["id"]))
            out = []
            for r in rows:
                vals = []
                for c in cols:
                    if c.endswith("AS parent"):
                        vals.append(self._parent(r))
                    else:
                        vals.append(r[c])
                        if c == "document":
                            self.bodies_read.add(r["id"])
                out.append(tuple(vals))
            return out
        if sql.startswith("SELECT id FROM mempalace_drawers WHERE id = ANY"):
            ids = set(params[0])
            return [(r["id"],) for r in self.rows if r["id"] in ids]
        if sql.startswith("SELECT id, document, metadata FROM mempalace_drawers WHERE id = ANY"):
            ids = set(params[0])
            hit = [r for r in self.rows if r["id"] in ids]
            self.bodies_read.update(r["id"] for r in hit)
            return [(r["id"], r["document"], r["metadata"]) for r in hit]
        if "ORDER BY id LIMIT %s OFFSET %s" in sql:
            rows, p = self._filtered(sql, params)
            limit, offset = p
            rows = sorted(rows, key=lambda r: r["id"])[offset : offset + limit]
            self.bodies_read.update(r["id"] for r in rows)
            return [(r["id"], r["document"], r["wing"], r["room"], r["metadata"]) for r in rows]
        raise AssertionError(f"unexpected SQL in the fake: {sql}")

    def connect(self, *a, **k):
        db = self

        class Cur:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def execute(self, sql, params=None):
                self._res = db.run(sql, params)

            def fetchall(self):
                return list(self._res)

            def fetchone(self):
                return self._res[0]

        class Conn:
            def __enter__(self):
                return self

            def __exit__(self, *e):
                return False

            def cursor(self):
                return Cur()

            def close(self):
                pass

        return Conn()


def _palace(n_singles=3, n_groups=5, chunks=4):
    rows = [{"id": f"s{i:03d}", "document": f"single {i}", "wing": "w", "room": "r", "metadata": {}}
            for i in range(n_singles)]
    for g in range(n_groups):
        for c in range(chunks):
            rows.append({"id": f"c{g:02d}-{c}", "document": f"group {g} chunk {c}", "wing": "w", "room": "r",
                         "metadata": {"parent_drawer_id": f"p{g:02d}", "chunk_index": c,
                                      "aaak_source": "x" * 1000}})
    return rows


class TestChunkSweepBounded(unittest.TestCase):
    """0a19023b / 8e81d5db (2026-11-27): the chunk-group sweep pulled every
    chunk row's document and metadata (~24 KB of aaak_source each, 20k rows)
    into Python: 1.5 GB RSS in 6 s on a cold cache. It now sweeps ids, wing
    and room only, and reads the first chunk's body for the page's orphan
    groups by id. The unfiltered anchor count no longer scans every row."""

    def setUp(self):
        fast_intercept._chunk_group_cache.update({"at": 0.0, "groups": None})
        self.addCleanup(fast_intercept._chunk_group_cache.update, {"at": 0.0, "groups": None})

    def _payload(self, db, **kw):
        with patch("psycopg2.connect", db.connect), patch("postgres.postgres_dsn", return_value="dsn"):
            return fast_intercept.fast_list_payload(**kw)

    def test_sweep_holds_no_documents_or_metadata(self):
        db = _FakeDrawers(_palace())
        groups = fast_intercept._load_chunk_groups(db.connect().cursor())
        self.assertEqual(len(groups), 5)
        self.assertEqual(groups["p00"]["chunk_ids"], ["c00-0", "c00-1", "c00-2", "c00-3"])
        held = json.dumps(groups, default=str)
        self.assertNotIn("aaak_source", held)
        self.assertNotIn("chunk 0", held)
        self.assertEqual(db.bodies_read, set())

    def test_orphan_page_reads_only_its_first_chunks(self):
        db = _FakeDrawers(_palace())
        p = self._payload(db, limit=2, offset=3)  # 3 singles, then orphan groups p00, p01
        self.assertEqual(p["total"], 8)
        self.assertEqual([d["drawer_id"] for d in p["drawers"]], ["p00", "p01"])
        self.assertEqual(p["drawers"][0]["content_preview"], "group 0 chunk 0")
        self.assertEqual(p["drawers"][1]["metadata"]["chunks"], 4)
        self.assertEqual(p["drawers"][1]["metadata"]["chunk_ids"], ["c01-0", "c01-1", "c01-2", "c01-3"])
        self.assertEqual(db.bodies_read, {"c00-0", "c01-0"})

    def test_page_across_the_boundary_matches(self):
        db = _FakeDrawers(_palace())
        p = self._payload(db, limit=3, offset=2)
        self.assertEqual([d["drawer_id"] for d in p["drawers"]], ["s002", "p00", "p01"])
        self.assertEqual(db.bodies_read, {"s002", "c00-0", "c01-0"})

    def test_unfiltered_anchor_count_never_scans_for_null_parent(self):
        db = _FakeDrawers(_palace())
        p = self._payload(db, limit=1, offset=0)
        self.assertEqual(p["total"], 8)
        counts = [s for s in db.sql if s.startswith("SELECT count(*)")]
        self.assertTrue(counts)
        self.assertFalse([s for s in counts if "IS NULL" in s], counts)

    def test_wing_filtered_count_unchanged(self):
        db = _FakeDrawers(_palace())
        p = self._payload(db, wing="w", limit=1, offset=0)
        self.assertEqual(p["total"], 8)
        db2 = _FakeDrawers(_palace())
        self.assertEqual(self._payload(db2, wing="nope", limit=1, offset=0)["total"], 0)


if __name__ == "__main__":
    unittest.main()
