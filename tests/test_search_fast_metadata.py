"""/search/fast must pass drawer provenance metadata through (palace-daemon#252).

The fast BM25 route returned only ``source_file`` + ``tags`` from a drawer's
metadata. Callers deciding whether a hit is a stale copy of a claim (the
refuted-claim incident: mempalace#451) need to know WHEN the drawer was filed
and when its source file was last modified, and which chunk of that file the
hit is. All three already live in ``metadata``; they were simply dropped on
the floor.

``created_at`` is the mempalace searcher's name for the miner's ``filed_at``
key (see ``mempalace/searcher.py``), with ``added_at`` as the older fallback.
Keeping the same name here means /search/fast hits and /search/keyword hits
can be compared without a per-route key map.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_search_fast_metadata.py -q
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows
        self.last_sql = None
        self.last_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def execute(self, sql, params=None):
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


def _row(meta, drawer_id="drawer_2g_general_01b8"):
    """One mempalace_drawers row in the column order /search/fast selects."""
    return (drawer_id, "2g", "general", json.dumps(meta), 0.42, "snippet text")


class SearchFastMetadataTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, rows):
        cur = _FakeCursor(rows)
        conn = _FakeConn(cur)
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgresql:///fake"}), \
             patch("psycopg2.connect", return_value=conn):
            return await main.search_fast(q="refuted", limit=5, wing=None, x_api_key=None)

    async def test_filed_at_surfaces_as_created_at(self):
        results = await self._run([_row({
            "source_file": "/home/jp/Projects/2g/CLAUDE.md",
            "filed_at": "2026-09-01T14:13:02.123456",
            "source_mtime": 1756744382.0,
            "chunk_index": 7,
        })])
        self.assertEqual(len(results), 1)
        hit = results[0]
        self.assertEqual(hit["created_at"], "2026-09-01T14:13:02.123456")
        self.assertEqual(hit["source_mtime"], 1756744382.0)
        self.assertEqual(hit["chunk_index"], 7)

    async def test_added_at_is_the_created_at_fallback(self):
        results = await self._run([_row({"added_at": "2026-04-01T00:00:00"})])
        self.assertEqual(results[0]["created_at"], "2026-04-01T00:00:00")

    async def test_missing_provenance_keys_are_none_not_absent(self):
        """A caller must be able to read the keys unconditionally."""
        results = await self._run([_row({"source_file": "/x/y.md"})])
        hit = results[0]
        for key in ("created_at", "source_mtime", "chunk_index"):
            self.assertIn(key, hit)
            self.assertIsNone(hit[key])

    async def test_existing_fields_are_unchanged(self):
        results = await self._run([_row({
            "source_file": "/x/y.md", "tags": ["a"], "filed_at": "2026-09-01T00:00:00",
        })])
        hit = results[0]
        self.assertEqual(hit["id"], "drawer_2g_general_01b8")
        self.assertEqual(hit["wing"], "2g")
        self.assertEqual(hit["room"], "general")
        self.assertEqual(hit["rank"], 0.42)
        self.assertEqual(hit["snippet"], "snippet text")
        self.assertEqual(hit["source_file"], "/x/y.md")
        self.assertEqual(hit["tags"], ["a"])


if __name__ == "__main__":
    unittest.main()
