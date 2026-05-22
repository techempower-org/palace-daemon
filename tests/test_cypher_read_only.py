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


if __name__ == "__main__":
    unittest.main()
