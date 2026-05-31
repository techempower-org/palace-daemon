"""Unit tests for POST /backfill-age/indexes (Cat 7b graph-walk latency fix).

The route installs the AGE edge-endpoint indexes the hybrid / age-fused
graph-walk paths need (MENTIONS + RELATION on start_id/end_id). The DDL runs
against a real postgres connection in production; these tests mock psycopg2 so
they need no DB, no palace, and no fixtures — they pin the idempotency
(already-present skip), the per-index error isolation, and the postgres-only
503 gate.

Run::

    cd /path/to/palace-daemon
    python -m unittest tests.test_backfill_age_indexes -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import backfill_routes  # noqa: E402

client = TestClient(main.app)


def _make_conn(existing_index_names):
    """A fake autocommit psycopg2 connection.

    The first cursor.fetchall() (the _existing_age_indexes probe) returns the
    pre-existing index names; subsequent CREATE INDEX executes are recorded so
    a test can assert which indexes the route tried to build.
    """
    conn = MagicMock()
    conn.closed = False
    executed = []

    def make_cursor():
        cur = MagicMock()
        cur.fetchall.return_value = [(n,) for n in existing_index_names]

        def _exec(sql, *a, **k):
            executed.append(sql)

        cur.execute.side_effect = _exec
        ctx = MagicMock()
        ctx.__enter__.return_value = cur
        ctx.__exit__.return_value = False
        return ctx

    conn.cursor.side_effect = make_cursor
    conn._executed = executed
    return conn


class TestBackfillAgeIndexes(unittest.TestCase):
    def test_creates_all_when_none_present(self):
        conn = _make_conn(existing_index_names=[])
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(
            set(body["created"]),
            {"idx_mentions_end_id", "idx_mentions_start_id",
             "idx_relation_start_id", "idx_relation_end_id"},
        )
        self.assertEqual(body["already_present"], [])
        self.assertEqual(body["errors"], {})
        # CONCURRENTLY must be used (online build, no table lock).
        creates = [s for s in conn._executed if "CREATE INDEX" in s]
        self.assertEqual(len(creates), 4)
        self.assertTrue(all("CONCURRENTLY" in s for s in creates))

    def test_idempotent_skips_present(self):
        conn = _make_conn(existing_index_names=[
            "idx_mentions_end_id", "idx_mentions_start_id",
            "idx_relation_start_id", "idx_relation_end_id",
        ])
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["created"], [])
        self.assertEqual(len(body["already_present"]), 4)
        # No CREATE INDEX should have been issued when all are present.
        self.assertFalse([s for s in conn._executed if "CREATE INDEX" in s])

    def test_partial_present_creates_only_missing(self):
        conn = _make_conn(existing_index_names=["idx_mentions_end_id"])
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = client.post("/backfill-age/indexes")
        body = r.json()
        self.assertEqual(body["already_present"], ["idx_mentions_end_id"])
        self.assertEqual(len(body["created"]), 3)
        self.assertNotIn("idx_mentions_end_id", body["created"])

    def test_per_index_error_does_not_sink_the_rest(self):
        # First CREATE raises, remaining succeed.
        conn = _make_conn(existing_index_names=[])
        calls = {"n": 0}
        cursors = []

        def make_cursor():
            cur = MagicMock()
            cur.fetchall.return_value = []

            def _exec(sql, *a, **k):
                if "CREATE INDEX" in sql:
                    calls["n"] += 1
                    if calls["n"] == 1:
                        raise RuntimeError("boom")

            cur.execute.side_effect = _exec
            ctx = MagicMock()
            ctx.__enter__.return_value = cur
            ctx.__exit__.return_value = False
            cursors.append(cur)
            return ctx

        conn.cursor.side_effect = make_cursor
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 200)  # some succeeded
        body = r.json()
        self.assertEqual(len(body["created"]), 3)
        self.assertEqual(len(body["errors"]), 1)

    def test_all_errors_returns_500(self):
        conn = _make_conn(existing_index_names=[])

        def make_cursor():
            cur = MagicMock()
            cur.fetchall.return_value = []

            def _exec(sql, *a, **k):
                if "CREATE INDEX" in sql:
                    raise RuntimeError("boom")

            cur.execute.side_effect = _exec
            ctx = MagicMock()
            ctx.__enter__.return_value = cur
            ctx.__exit__.return_value = False
            return ctx

        conn.cursor.side_effect = make_cursor
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 500)

    def test_503_on_chroma_backend(self):
        with patch.object(main, "_check_auth"), \
             patch.object(main._mp, "_config", MagicMock(backend="chroma")):
            r = client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 503)

    def test_index_ddl_targets_are_complete(self):
        # Guard: the four edge-endpoint indexes the graph-walk paths join on.
        names = {name for name, _ in backfill_routes._AGE_INDEX_DDL}
        self.assertEqual(
            names,
            {"idx_mentions_end_id", "idx_mentions_start_id",
             "idx_relation_start_id", "idx_relation_end_id"},
        )
        for _, ddl in backfill_routes._AGE_INDEX_DDL:
            self.assertIn("CONCURRENTLY", ddl)
            self.assertIn("IF NOT EXISTS", ddl)

    def test_presence_probe_filters_invalid_indexes(self):
        # Gemini review: a failed CREATE INDEX CONCURRENTLY leaves an INVALID
        # index that pg_indexes lists but the planner can't use. The probe must
        # check pg_index.indisvalid so an invalid index is NOT counted present
        # (otherwise the rebuild is silently skipped and the latency bug stays).
        conn = _make_conn(existing_index_names=[])
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            client.post("/backfill-age/indexes")
        probe = [s for s in conn._executed
                 if "pg_index" in s and "relname" in s]
        self.assertTrue(probe, "presence probe should query pg_index, not the pg_indexes view")
        self.assertIn("indisvalid", probe[0])

    def test_connection_closed_when_autocommit_raises(self):
        # Gemini review: if connect() succeeds but setting autocommit raises,
        # the connection must still be closed (no leak). The route initializes
        # conn=None and wraps the whole setup in one try/finally, so the close
        # runs even though the exception then propagates out of _run.
        conn = MagicMock()
        conn.closed = False

        def _raise(self, v):
            raise RuntimeError("autocommit boom")

        type(conn).autocommit = property(lambda self: False, _raise)
        # Server exceptions surface as 500 instead of re-raising into the test.
        local_client = TestClient(main.app, raise_server_exceptions=False)
        with patch.object(main, "_check_auth"), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": "postgres://fake"}), \
             patch.object(main._mp, "_config", MagicMock(backend="postgres")), \
             patch("psycopg2.connect", return_value=conn):
            r = local_client.post("/backfill-age/indexes")
        self.assertEqual(r.status_code, 500)
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
