"""Unit tests for `/graph` wing/room source dispatch.

Pinpoints the chroma → postgres regression that produced stale wing
counts (familiar_realm_watch reporting 25 drawers when postgres held
235): `_read_wings_rooms_direct` was unconditionally reading the
chroma.sqlite3 snapshot even after MEMPALACE_BACKEND=postgres made it a
frozen pre-migration store. These tests check the dispatch table, not
the live backend behavior — they monkeypatch `_mp._config.backend` and
the helpers, so they need no palace, no postgres, and no chroma file.

Run::

    cd /path/to/palace-daemon
    python -m unittest tests.test_graph_wings_dispatch -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import kg_reader  # noqa: E402


class _Cfg:
    def __init__(self, backend):
        self.backend = backend


class TestReadWingsRoomsDispatch(unittest.TestCase):
    def test_postgres_backend_uses_postgres_helper(self):
        """Under MEMPALACE_BACKEND=postgres, dispatch must call the
        postgres helper — never the chroma sqlite snapshot, which is a
        stale pre-migration store under this backend.
        """
        sentinel = ({"familiar_realm_watch": 235}, [
            {"wing": "familiar_realm_watch", "rooms": {"architecture": 40}}
        ])
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_wings_rooms_postgres", return_value=sentinel) as pg, \
             patch("sqlite3.connect") as sql_connect:
            wings, rooms = main._read_wings_rooms_direct()
        self.assertEqual(pg.call_count, 1)
        # No sqlite open under postgres backend — the chroma path is the
        # bug we're fixing.
        sql_connect.assert_not_called()
        self.assertEqual(wings, {"familiar_realm_watch": 235})
        self.assertEqual(rooms, sentinel[1])

    def test_chroma_backend_does_not_call_postgres(self):
        """Under MEMPALACE_BACKEND=chroma the legacy sqlite path is
        still authoritative; the postgres helper must not be invoked.
        """
        with patch.object(kg_reader, "_config", return_value=_Cfg("chroma")), \
             patch.object(kg_reader, "read_wings_rooms_postgres") as pg, \
             patch.object(kg_reader, "chroma_path", return_value="/nonexistent/chroma.sqlite3"):
            wings, rooms = main._read_wings_rooms_direct()
        pg.assert_not_called()
        # File doesn't exist → degrade to empty (the documented behavior).
        self.assertEqual(wings, {})
        self.assertEqual(rooms, [])

    def test_unknown_backend_does_not_call_postgres(self):
        """If `_mp._config.backend` is missing or unrecognized, fall
        through to the chroma sqlite path rather than risk hitting a
        misconfigured postgres connection.
        """
        with patch.object(kg_reader, "_config", return_value=_Cfg(None)), \
             patch.object(kg_reader, "read_wings_rooms_postgres") as pg, \
             patch.object(kg_reader, "chroma_path", return_value="/nonexistent/chroma.sqlite3"):
            main._read_wings_rooms_direct()
        pg.assert_not_called()


class TestReadKgDirectDispatch(unittest.TestCase):
    def test_postgres_backend_dispatches_to_age_helper(self):
        """Under postgres backend the KG lives in AGE; dispatch must
        route to `_read_kg_postgres` (which queries live AGE), never to
        the sibling knowledge_graph.sqlite3 (which is a frozen pre-
        migration leftover under this backend).
        """
        sentinel_entities = [{"id": "Razer Kiyo Pro", "name": "Razer Kiyo Pro", "type": "entity", "properties": {}}]
        sentinel_triples = [{
            "subject": "Razer Kiyo Pro", "predicate": "status_byte",
            "object": "0x82", "valid_from": "2026-04-11",
            "valid_to": None, "confidence": 1, "source_file": None,
        }]
        sentinel_mentions = [{
            "subject": "drawer-9", "predicate": "MENTIONS",
            "object": "Razer Kiyo Pro", "valid_from": None,
            "valid_to": None, "confidence": 0.5, "source_file": "PROPER_NOUN",
        }]
        sentinel = (sentinel_entities, sentinel_triples, sentinel_mentions)
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres", return_value=sentinel) as pg, \
             patch("sqlite3.connect") as sql_connect:
            entities, triples, mentions = main._read_kg_direct()
        self.assertEqual(pg.call_count, 1)
        # The chroma KG sqlite file must NOT be opened under postgres backend
        # — that path is the staleness bug we're fixing.
        sql_connect.assert_not_called()
        self.assertEqual(entities, sentinel_entities)
        self.assertEqual(triples, sentinel_triples)
        self.assertEqual(mentions, sentinel_mentions)

    def test_chroma_backend_does_not_call_age_helper(self):
        """Under MEMPALACE_BACKEND=chroma the legacy sqlite KG path is
        authoritative; the AGE helper must not be invoked. mentions is
        always empty under chroma (no MENTIONS concept in the sqlite KG).
        """
        with patch.object(kg_reader, "_config", return_value=_Cfg("chroma")), \
             patch.object(kg_reader, "read_kg_postgres") as pg, \
             patch.object(kg_reader, "kg_path", return_value="/nonexistent/knowledge_graph.sqlite3"):
            entities, triples, mentions = main._read_kg_direct()
        pg.assert_not_called()
        # File doesn't exist → degrade to empty.
        self.assertEqual(entities, [])
        self.assertEqual(triples, [])
        self.assertEqual(mentions, [])


class TestReadKgPostgresAGE(unittest.TestCase):
    """`_read_kg_postgres` (1.8.0+) reads from AGE label tables.

    Post-#160 the implementation switched from Cypher walks (which
    exhaust postgres shared memory at production scale) to direct
    SQL on the backing label tables with a CTE that bounds the edge
    scan before the Entity/Drawer join.

    These tests stub a psycopg2 connection so the wiring (which
    queries run, what LIMITs they carry, how rows project) is pinned
    without needing a live Postgres + AGE.
    """

    def _make_pg(self, ent_rows, rel_rows, men_rows):
        """Build a fake psycopg2 connection capturing executed SQL +
        returning the staged rows based on which table is referenced.

        The kg_reader code issues three SELECTs; we dispatch on substrings
        in the SQL ("FROM ...Entity\"", "FROM rel_sample", etc.) so the
        test stays decoupled from exact whitespace.
        """
        from unittest.mock import MagicMock

        captured = {"calls": []}

        class _FakeCursor:
            def __init__(self):
                self._rows = []

            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def execute(self, sql, params=None):
                captured["calls"].append((sql, params))
                # Bare SETs return no rows.
                if sql.strip().upper().startswith("SET"):
                    self._rows = []
                    return
                # Project to the staged rows by table reference.
                if 'FROM rel_sample' in sql or '"RELATION"' in sql and 'rel_sample' in sql:
                    self._rows = list(rel_rows)
                elif 'FROM men_sample' in sql or '"MENTIONS"' in sql and 'men_sample' in sql:
                    self._rows = list(men_rows)
                elif '"Entity"' in sql:
                    self._rows = list(ent_rows)
                else:
                    self._rows = []

            def fetchall(self):
                return self._rows

        class _FakeConn:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                pass

            def cursor(self):
                return _FakeCursor()

            def rollback(self):
                pass

            def close(self):
                pass

        return _FakeConn, captured

    def _make_stub_kg(self):
        """The new code still instantiates KnowledgeGraphAGE just to read
        GRAPH_NAME from it. Stub it so the import resolves."""
        class _StubKG:
            GRAPH_NAME = "mempalace_kg"

            def __init__(self, dsn=None):
                self.dsn = dsn

            def close(self):
                pass

        return _StubKG

    def test_age_projection_and_limits(self):
        """Three SQL queries fire after the SET statements: Entity scan,
        RELATION CTE+join, MENTIONS CTE+join. Each gets its own ``LIMIT``
        from a distinct kwarg so /graph can size them independently.
        """
        ent_rows = [("alpha",), ("beta",)]
        rel_rows = [("alpha", "works_at", "beta", "1.0")]
        men_rows = [
            ("drawer-1", "alpha", "PROPER_NOUN", "0.5"),
            ("drawer-2", "beta", "TECH_IDENT", "0.5"),
        ]
        FakeConn, captured = self._make_pg(ent_rows, rel_rows, men_rows)
        StubKG = self._make_stub_kg()

        import sys
        stub_mod = type(sys)("mempalace.knowledge_graph_age")
        stub_mod.KnowledgeGraphAGE = StubKG
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = "postgres://stub"
        with patch.dict(sys.modules, {"mempalace.knowledge_graph_age": stub_mod}), \
             patch.object(kg_reader, "_config", return_value=cfg), \
             patch("psycopg2.connect", return_value=FakeConn()):
            entities, triples, mentions = main._read_kg_postgres(
                entity_limit=50, triple_limit=10, mention_limit=120
            )

        # Filter out the SET statements — three data queries remain.
        data_calls = [
            (sql, params) for sql, params in captured["calls"]
            if not sql.strip().upper().startswith("SET")
        ]
        self.assertEqual(len(data_calls), 3)

        ent_sql, ent_params = data_calls[0]
        self.assertIn('"Entity"', ent_sql)
        self.assertEqual(ent_params, (50,))

        rel_sql, rel_params = data_calls[1]
        self.assertIn('rel_sample', rel_sql)
        self.assertIn('"RELATION"', rel_sql)
        self.assertEqual(rel_params, (10,))

        men_sql, men_params = data_calls[2]
        self.assertIn('men_sample', men_sql)
        self.assertIn('"MENTIONS"', men_sql)
        self.assertEqual(men_params, (120,))

        self.assertEqual(entities, [
            {"id": "alpha", "name": "alpha", "type": "entity", "properties": {}},
            {"id": "beta", "name": "beta", "type": "entity", "properties": {}},
        ])
        self.assertEqual(triples, [
            {
                "subject": "alpha", "predicate": "works_at",
                "object": "beta", "valid_from": None, "valid_to": None,
                "confidence": 1.0, "source_file": None,
            },
        ])
        self.assertEqual(mentions, [
            {
                "subject": "drawer-1", "predicate": "MENTIONS",
                "object": "alpha", "valid_from": None, "valid_to": None,
                "confidence": 0.5, "source_file": "PROPER_NOUN",
            },
            {
                "subject": "drawer-2", "predicate": "MENTIONS",
                "object": "beta", "valid_from": None, "valid_to": None,
                "confidence": 0.5, "source_file": "TECH_IDENT",
            },
        ])

    def test_age_no_dsn_degrades_to_empty(self):
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = None
        with patch.object(kg_reader, "_config", return_value=cfg), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": ""}, clear=False):
            entities, triples, mentions = main._read_kg_postgres()
        self.assertEqual(entities, [])
        self.assertEqual(triples, [])
        self.assertEqual(mentions, [])


class TestReadKgStatsAGE(unittest.TestCase):
    """`_read_kg_postgres_stats` pins the 1.8.2 schema: `/graph` `kg_stats`
    splits the entity / RELATION-triple / MENTIONS-edge counts into
    three separate fields. Pre-1.8.2 the helper counted MENTIONS and
    labeled them ``triples``, hiding the fact that the corpus has ~zero
    real semantic facts.

    Implementation note: the helper avoids Cypher (`MATCH ()-[r:MENTIONS]->()
    RETURN count(r)`) because AGE materializes the full edge scan and
    exhausts Postgres shared memory at 5M+ rows. It SELECT-counts the
    backing label tables in the `mempalace_kg` schema instead. These
    tests stub `kg._conn.cursor()` to capture the SQL and feed counts
    back, no live Postgres needed.
    """

    def _make_kg_class(
        self, entity_count, triple_count, mentions_count, raise_on=None
    ):
        captured = {"sql": []}

        class _StubCursor:
            def __init__(self):
                self._last = None
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def execute(self, sql, params=None):
                captured["sql"].append(sql)
                if raise_on and raise_on in sql:
                    raise RuntimeError("simulated backing-table failure")
                if '"Entity"' in sql:
                    self._last = (entity_count,)
                elif '"RELATION"' in sql:
                    self._last = (triple_count,)
                elif '"MENTIONS"' in sql:
                    self._last = (mentions_count,)
                else:
                    self._last = (0,)
            def fetchone(self):
                return self._last

        class _StubConn:
            def cursor(self):
                return _StubCursor()
            def rollback(self):
                captured["sql"].append("ROLLBACK")

        class _StubKG:
            GRAPH_NAME = "mempalace_kg"
            def __init__(self, dsn=None):
                self.dsn = dsn
                self._conn = _StubConn()
            def close(self):
                pass

        return _StubKG, captured

    def test_age_stats_projection(self):
        """Three separate counts in three separate fields. SQL must be
        table-scoped to the graph's schema with quoted-identifier names
        (AGE preserves case). `relationship_types` reports only the
        nonzero edge labels so a consumer can tell what's populated
        without crawling the counts.
        """
        StubKG, captured = self._make_kg_class(267_519, 1, 5_580_000)
        import sys
        stub_mod = type(sys)("mempalace.knowledge_graph_age")
        stub_mod.KnowledgeGraphAGE = StubKG
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = "postgres://stub"
        with patch.dict(sys.modules, {"mempalace.knowledge_graph_age": stub_mod}), \
             patch.object(kg_reader, "_config", return_value=cfg):
            stats = main._read_kg_postgres_stats()
        self.assertEqual(len(captured["sql"]), 3)
        self.assertIn('mempalace_kg."Entity"', captured["sql"][0])
        self.assertIn('mempalace_kg."RELATION"', captured["sql"][1])
        self.assertIn('mempalace_kg."MENTIONS"', captured["sql"][2])
        self.assertEqual(stats, {
            "entities": 267_519,
            "triples": 1,
            "mentions": 5_580_000,
            "relationship_types": ["RELATION", "MENTIONS"],
        })

    def test_age_stats_relation_types_drops_empty_labels(self):
        """When RELATION is empty (the current corpus state),
        relationship_types should list only MENTIONS — not advertise a
        label with zero rows.
        """
        StubKG, _ = self._make_kg_class(100, 0, 5_000)
        import sys
        stub_mod = type(sys)("mempalace.knowledge_graph_age")
        stub_mod.KnowledgeGraphAGE = StubKG
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = "postgres://stub"
        with patch.dict(sys.modules, {"mempalace.knowledge_graph_age": stub_mod}), \
             patch.object(kg_reader, "_config", return_value=cfg):
            stats = main._read_kg_postgres_stats()
        self.assertEqual(stats["relationship_types"], ["MENTIONS"])
        self.assertEqual(stats["triples"], 0)
        self.assertEqual(stats["mentions"], 5_000)

    def test_age_stats_no_dsn_returns_none(self):
        """No DSN → return None so the `/graph` handler falls back to
        the MCP `kg_stats` path rather than reporting bogus zeros.
        """
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = None
        with patch.object(kg_reader, "_config", return_value=cfg), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": ""}, clear=False):
            stats = main._read_kg_postgres_stats()
        self.assertIsNone(stats)

    def test_age_stats_sql_failure_preserves_partial_truth(self):
        """A SQL exception mid-sequence must not bubble. Counts that
        succeeded before the raise are preserved (partial truth beats
        wiping known-good data); the failed counter and any later ones
        degrade to 0. rollback() must fire so the shared psycopg2
        connection isn't left in an aborted txn state (which would
        poison every subsequent /graph call).
        """
        # Entity succeeds (10), RELATION raises before MENTIONS runs
        StubKG, captured = self._make_kg_class(
            entity_count=10, triple_count=5, mentions_count=20,
            raise_on='"RELATION"',
        )
        import sys
        stub_mod = type(sys)("mempalace.knowledge_graph_age")
        stub_mod.KnowledgeGraphAGE = StubKG
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = "postgres://stub"
        with patch.dict(sys.modules, {"mempalace.knowledge_graph_age": stub_mod}), \
             patch.object(kg_reader, "_config", return_value=cfg):
            stats = main._read_kg_postgres_stats()
        self.assertEqual(stats, {
            "entities": 10,
            "triples": 0,
            "mentions": 0,
            "relationship_types": [],
        })
        self.assertIn("ROLLBACK", captured["sql"])


class TestReadKgStatsDirectDispatch(unittest.TestCase):
    def test_postgres_backend_dispatches_to_age_stats(self):
        sentinel = {
            "entities": 1, "triples": 0, "mentions": 5_000_000,
            "relationship_types": ["MENTIONS"],
        }
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres_stats", return_value=sentinel) as pg:
            stats = main._read_kg_stats_direct()
        pg.assert_called_once()
        self.assertIs(stats, sentinel)

    def test_chroma_backend_returns_none(self):
        """Under chroma the legacy MCP `kg_stats` is still authoritative;
        the dispatcher must return None so `/graph` falls back to it
        instead of forcing the postgres helper.
        """
        with patch.object(kg_reader, "_config", return_value=_Cfg("chroma")), \
             patch.object(kg_reader, "read_kg_postgres_stats") as pg:
            stats = main._read_kg_stats_direct()
        pg.assert_not_called()
        self.assertIsNone(stats)


if __name__ == "__main__":
    unittest.main()
