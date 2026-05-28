"""Regression tests for the /cypher endpoint's read-only enforcement.

Locks in the fix for issue #30: the daemon must execute caller-supplied
Cypher inside a PostgreSQL read-only transaction so that AGE write verbs
(``CREATE``/``MERGE``/``SET``/``DELETE``/``DETACH DELETE``/``REMOVE``)
fail at the database layer regardless of the daemon-side SQL surface
(which can't introspect AGE's dollar-quoted Cypher payload).

We mock ``KnowledgeGraphAGE`` so the test runs without a live Postgres /
AGE — we only need to assert that:

  1. The endpoint issues ``SET TRANSACTION READ ONLY`` before invoking
     ``_run_cypher``.
  2. A ``psycopg2.errors.ReadOnlySqlTransaction`` raised from inside
     ``_run_cypher`` is translated into HTTP 403 (not 500).

Run with::

    cd /home/jp/Projects/palace-daemon
    source venv/bin/activate
    python -m unittest tests.test_cypher_read_only -v
"""
import asyncio
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import psycopg2.errors  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402


def _fake_request(body: dict):
    """Build a minimal stand-in for fastapi.Request that yields ``body``."""
    req = MagicMock()

    async def _json():
        return body

    req.json = _json
    return req


class _StubKG:
    """Stand-in for KnowledgeGraphAGE that records cursor SQL and routes
    ``_run_cypher`` through a behavior hook so each test can pick its
    own outcome."""

    def __init__(self, dsn=None):
        self.dsn = dsn
        self.executed: list[str] = []
        self._conn = MagicMock()
        self._conn.cursor.return_value.__enter__.return_value = self
        self._conn.cursor.return_value.__exit__.return_value = False

    # cursor stand-in — KG's _run_cypher would call .execute on the
    # real psycopg2 cursor; we don't need to model that because
    # _run_cypher is also mocked. We only need .execute() recording
    # for the SET TRANSACTION READ ONLY assertion.
    def execute(self, sql, *args, **kwargs):
        self.executed.append(sql)

    def _run_cypher(self, cypher, params=None, fetch=False):
        raise AssertionError("test must set self._run_cypher_impl")

    def _extract_return_aliases(self, cypher):
        return ["v"]

    @staticmethod
    def _unwrap_agtype(val):
        return val

    def close(self):
        pass


class TestCypherReadOnly(unittest.TestCase):
    def _run_cypher_endpoint(self, kg_factory):
        """Invoke main.cypher_query with the postgres backend + DSN
        stubbed and ``KnowledgeGraphAGE`` swapped for ``kg_factory``."""
        # ``cypher_query``'s inner ``_run`` re-imports
        # ``mempalace.knowledge_graph_age``; install a fake module so the
        # import succeeds and resolves to our stub.
        fake_mod = types.ModuleType("mempalace.knowledge_graph_age")
        fake_mod.KnowledgeGraphAGE = kg_factory
        mempalace_pkg = types.ModuleType("mempalace")
        mempalace_pkg.knowledge_graph_age = fake_mod

        # Fake _mp._config — only the two attributes the endpoint reads.
        fake_config = types.SimpleNamespace(
            backend="postgres", postgres_dsn="postgresql://fake/db"
        )
        fake_mp = types.SimpleNamespace(_config=fake_config)

        with patch.dict(
            sys.modules,
            {"mempalace": mempalace_pkg, "mempalace.knowledge_graph_age": fake_mod},
            clear=False,
        ), patch.object(main, "_mp", fake_mp), patch.dict(
            os.environ, {}, clear=True
        ):  # no PALACE_API_KEY so _check_auth is a no-op
            req = _fake_request({"cypher": "MATCH (n) RETURN n"})
            return asyncio.run(main.cypher_query(req, x_api_key=None))

    def test_sets_transaction_read_only_before_cypher(self):
        """Endpoint must issue ``SET TRANSACTION READ ONLY`` before
        running the user-supplied Cypher."""
        captured = {}

        class _RecordingKG(_StubKG):
            def _run_cypher(self, cypher, params=None, fetch=False):
                captured["executed_at_run_cypher"] = list(self.executed)
                return [(42,)]

        result = self._run_cypher_endpoint(_RecordingKG)
        self.assertEqual(result["rows"], [{"v": 42}])
        # SET TRANSACTION READ ONLY must have already run by the time
        # _run_cypher was called.
        self.assertIn(
            "SET TRANSACTION READ ONLY", captured["executed_at_run_cypher"]
        )

    def test_read_only_violation_returns_403(self):
        """A ``ReadOnlySqlTransaction`` from _run_cypher must surface as
        HTTP 403, not 500, and the detail must name the rejected verbs."""

        class _WriteAttemptKG(_StubKG):
            def _run_cypher(self, cypher, params=None, fetch=False):
                raise psycopg2.errors.ReadOnlySqlTransaction(
                    "cannot execute MERGE in a read-only transaction"
                )

        with self.assertRaises(HTTPException) as ctx:
            self._run_cypher_endpoint(_WriteAttemptKG)
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("read-only", ctx.exception.detail.lower())


class TestCypherStructuredErrors(unittest.TestCase):
    """Postgres-side errors must surface as specific HTTP codes with
    structured detail — not a generic 500 with a stringified exception.
    Added after the 2026-05-28 deploy surfaced /cypher returning
    `Internal Server Error` for shared-memory exhaustion (a backend
    resource limit, not a daemon bug).
    """

    def _run_with_error(self, exc):
        """Run the endpoint with _run_cypher raising the given psycopg2
        exception. Returns the HTTPException raised by the endpoint."""

        class _FailingKG(_StubKG):
            def _run_cypher(self_inner, cypher, params=None, fetch=False):
                raise exc

        fake_mod = types.ModuleType("mempalace.knowledge_graph_age")
        fake_mod.KnowledgeGraphAGE = _FailingKG
        mempalace_pkg = types.ModuleType("mempalace")
        mempalace_pkg.knowledge_graph_age = fake_mod

        fake_config = types.SimpleNamespace(
            backend="postgres", postgres_dsn="postgresql://fake/db"
        )
        fake_mp = types.SimpleNamespace(_config=fake_config)

        with patch.dict(
            sys.modules,
            {"mempalace": mempalace_pkg, "mempalace.knowledge_graph_age": fake_mod},
            clear=False,
        ), patch.object(main, "_mp", fake_mp), patch.dict(
            os.environ, {}, clear=True
        ):
            req = _fake_request({"cypher": "MATCH (n) RETURN n"})
            try:
                asyncio.run(main.cypher_query(req, x_api_key=None))
            except HTTPException as e:
                return e
            self.fail("expected HTTPException")

    def test_out_of_memory_returns_507(self):
        """Shared-memory exhaustion → 507 (Insufficient Storage) with hint."""
        err = self._run_with_error(
            psycopg2.errors.OutOfMemory("could not resize shared memory segment ...")
        )
        self.assertEqual(err.status_code, 507)
        self.assertEqual(err.detail["error"], "shared-memory-exhausted")
        self.assertIn("CTE", err.detail["hint"])

    def test_query_canceled_returns_504(self):
        """statement_timeout → 504 (Gateway Timeout) with hint."""
        err = self._run_with_error(
            psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
        )
        self.assertEqual(err.status_code, 504)
        self.assertEqual(err.detail["error"], "timeout")
        self.assertIn("LIMIT", err.detail["hint"])

    def test_syntax_error_returns_400(self):
        """Bad Cypher → 400 with the postgres message."""
        err = self._run_with_error(
            psycopg2.errors.SyntaxError('syntax error at or near "AS"')
        )
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.detail["error"], "bad-query")
        self.assertIn("AS", err.detail["postgres"])

    def test_undefined_column_returns_400(self):
        """Schema mismatch → 400 (not 500) — the operator's query was wrong."""
        err = self._run_with_error(
            psycopg2.errors.UndefinedColumn("could not find rte for n")
        )
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.detail["error"], "bad-query")

    def test_generic_postgres_error_returns_502(self):
        """Other postgres errors → 502 (Bad Gateway) with structured body."""
        err = self._run_with_error(psycopg2.Error("connection reset"))
        self.assertEqual(err.status_code, 502)
        self.assertEqual(err.detail["error"], "postgres-error")
        self.assertIn("connection reset", err.detail["postgres"])


class TestCypherStructuredErrorsPsycopg3(unittest.TestCase):
    """The mempalace AGE helper uses psycopg v3 internally, so live errors
    surface as ``psycopg.errors.*`` — not ``psycopg2.errors.*``. PR #162
    initially only caught the psycopg2 variants and quietly didn't change
    live behavior; PR #163 added the v3 catches. These tests pin the
    psycopg3 mappings so a future library-version drift can't silently
    regress.

    Skipped if psycopg (v3) isn't installed — the daemon's primary
    requirement is psycopg2, v3 is via mempalace.
    """

    try:
        import psycopg as _pg
        import psycopg.errors as _pg_err
        HAS_PSYCOPG3 = True
    except ImportError:
        HAS_PSYCOPG3 = False

    def setUp(self):
        if not self.HAS_PSYCOPG3:
            self.skipTest("psycopg (v3) not installed; live daemon imports it via mempalace")

    def _run_with_error(self, exc):
        """Same as TestCypherStructuredErrors._run_with_error but in a
        separate class so the skipTest contract is clearer."""

        class _FailingKG(_StubKG):
            def _run_cypher(self_inner, cypher, params=None, fetch=False):
                raise exc

        fake_mod = types.ModuleType("mempalace.knowledge_graph_age")
        fake_mod.KnowledgeGraphAGE = _FailingKG
        mempalace_pkg = types.ModuleType("mempalace")
        mempalace_pkg.knowledge_graph_age = fake_mod

        fake_config = types.SimpleNamespace(
            backend="postgres", postgres_dsn="postgresql://fake/db"
        )
        fake_mp = types.SimpleNamespace(_config=fake_config)

        with patch.dict(
            sys.modules,
            {"mempalace": mempalace_pkg, "mempalace.knowledge_graph_age": fake_mod},
            clear=False,
        ), patch.object(main, "_mp", fake_mp), patch.dict(
            os.environ, {}, clear=True
        ):
            req = _fake_request({"cypher": "MATCH (n) RETURN n"})
            try:
                asyncio.run(main.cypher_query(req, x_api_key=None))
            except HTTPException as e:
                return e
            self.fail("expected HTTPException")

    def test_psycopg3_out_of_memory_returns_507(self):
        """The v3 OutOfMemory must also produce 507 — was a 500 in
        production prior to PR #163 because only psycopg2 was caught."""
        import psycopg.errors as pg_err
        err = self._run_with_error(
            pg_err.OutOfMemory("could not resize shared memory segment ...")
        )
        self.assertEqual(err.status_code, 507)
        self.assertEqual(err.detail["error"], "shared-memory-exhausted")

    def test_psycopg3_query_canceled_returns_504(self):
        """v3 QueryCanceled (statement_timeout) → 504."""
        import psycopg.errors as pg_err
        err = self._run_with_error(
            pg_err.QueryCanceled("canceling statement due to statement timeout")
        )
        self.assertEqual(err.status_code, 504)
        self.assertEqual(err.detail["error"], "timeout")

    def test_psycopg3_syntax_error_returns_400(self):
        """v3 SyntaxError → 400 (the actual error live /cypher hits when
        callers send malformed Cypher — confirmed by curl post-deploy)."""
        import psycopg.errors as pg_err
        err = self._run_with_error(
            pg_err.SyntaxError('syntax error at or near "AS"')
        )
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.detail["error"], "bad-query")

    def test_psycopg3_undefined_column_returns_400(self):
        """v3 UndefinedColumn → 400."""
        import psycopg.errors as pg_err
        err = self._run_with_error(
            pg_err.UndefinedColumn("could not find rte for n")
        )
        self.assertEqual(err.status_code, 400)
        self.assertEqual(err.detail["error"], "bad-query")

    def test_psycopg3_generic_error_returns_502(self):
        """v3 base Error → 502."""
        import psycopg as pg
        err = self._run_with_error(pg.Error("connection lost"))
        self.assertEqual(err.status_code, 502)
        self.assertEqual(err.detail["error"], "postgres-error")
        self.assertIn("connection lost", err.detail["postgres"])

    def test_psycopg3_read_only_returns_403(self):
        """v3 ReadOnlySqlTransaction must still hit the 403 path —
        was broken pre-#163 because the daemon imported psycopg2 in the
        original handler."""
        import psycopg.errors as pg_err
        err = self._run_with_error(
            pg_err.ReadOnlySqlTransaction("cannot execute MERGE")
        )
        self.assertEqual(err.status_code, 403)
        self.assertIn("read-only", err.detail.lower())


if __name__ == "__main__":
    unittest.main()
