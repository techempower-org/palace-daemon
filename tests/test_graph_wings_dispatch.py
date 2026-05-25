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
        with patch.object(main, "_mp") as mp, \
             patch.object(main, "_read_wings_rooms_postgres", return_value=sentinel) as pg, \
             patch("sqlite3.connect") as sql_connect:
            mp._config = _Cfg("postgres")
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
        with patch.object(main, "_mp") as mp, \
             patch.object(main, "_read_wings_rooms_postgres") as pg, \
             patch.object(main, "_chroma_path", return_value="/nonexistent/chroma.sqlite3"):
            mp._config = _Cfg("chroma")
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
        with patch.object(main, "_mp") as mp, \
             patch.object(main, "_read_wings_rooms_postgres") as pg, \
             patch.object(main, "_chroma_path", return_value="/nonexistent/chroma.sqlite3"):
            mp._config = _Cfg(None)
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
        sentinel = (sentinel_entities, sentinel_triples)
        with patch.object(main, "_mp") as mp, \
             patch.object(main, "_read_kg_postgres", return_value=sentinel) as pg, \
             patch("sqlite3.connect") as sql_connect:
            mp._config = _Cfg("postgres")
            entities, triples = main._read_kg_direct()
        self.assertEqual(pg.call_count, 1)
        # The chroma KG sqlite file must NOT be opened under postgres backend
        # — that path is the staleness bug we're fixing.
        sql_connect.assert_not_called()
        self.assertEqual(entities, sentinel_entities)
        self.assertEqual(triples, sentinel_triples)

    def test_chroma_backend_does_not_call_age_helper(self):
        """Under MEMPALACE_BACKEND=chroma the legacy sqlite KG path is
        authoritative; the AGE helper must not be invoked.
        """
        with patch.object(main, "_mp") as mp, \
             patch.object(main, "_read_kg_postgres") as pg, \
             patch.object(main, "_kg_path", return_value="/nonexistent/knowledge_graph.sqlite3"):
            mp._config = _Cfg("chroma")
            entities, triples = main._read_kg_direct()
        pg.assert_not_called()
        # File doesn't exist → degrade to empty.
        self.assertEqual(entities, [])
        self.assertEqual(triples, [])


class TestReadKgPostgresAGE(unittest.TestCase):
    """`_read_kg_postgres` (1.8.0+) runs Cypher against AGE for entities
    and Drawer→Entity MENTIONS edges. These tests stub the
    ``KnowledgeGraphAGE`` import so the wiring (which queries run, what
    LIMITs they carry, how rows project into the public schema) is
    pinned without needing a live Postgres + AGE.
    """

    def _make_kg_class(self, ent_rows, trip_rows):
        captured = {"calls": []}

        class _StubKG:
            def __init__(self, dsn=None):
                self.dsn = dsn

            def _run_cypher(self, cypher, params, fetch=True):
                captured["calls"].append((cypher, dict(params)))
                if "Entity)" in cypher and "MENTIONS" not in cypher:
                    return ent_rows
                return trip_rows

            @staticmethod
            def _unwrap_agtype(v):
                return v

            def close(self):
                pass

        return _StubKG, captured

    def test_age_projection_and_limits(self):
        ent_rows = [["alpha"], ["beta"]]
        trip_rows = [
            ["drawer-1", "alpha", 2, "PROPER_NOUN", 0.5],
            ["drawer-2", "beta", 1, "TECH_IDENT", 0.5],
        ]
        StubKG, captured = self._make_kg_class(ent_rows, trip_rows)

        import sys
        stub_mod = type(sys)("mempalace.knowledge_graph_age")
        stub_mod.KnowledgeGraphAGE = StubKG
        with patch.dict(sys.modules, {"mempalace.knowledge_graph_age": stub_mod}), \
             patch.object(main, "_mp") as mp:
            mp._config = _Cfg("postgres")
            mp._config.postgres_dsn = "postgres://stub"
            entities, triples = main._read_kg_postgres(
                entity_limit=50, triple_limit=120
            )

        self.assertEqual(len(captured["calls"]), 2)
        ent_call_cypher, ent_params = captured["calls"][0]
        self.assertIn("MATCH (e:Entity)", ent_call_cypher)
        self.assertEqual(ent_params, {"n": 50})
        trip_call_cypher, trip_params = captured["calls"][1]
        self.assertIn("(d:Drawer)-[r:MENTIONS]->(e:Entity)", trip_call_cypher)
        self.assertEqual(trip_params, {"n": 120})

        self.assertEqual(entities, [
            {"id": "alpha", "name": "alpha", "type": "entity", "properties": {}},
            {"id": "beta", "name": "beta", "type": "entity", "properties": {}},
        ])
        self.assertEqual(triples, [
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
        with patch.object(main, "_mp") as mp, \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": ""}, clear=False):
            mp._config = _Cfg("postgres")
            mp._config.postgres_dsn = None
            entities, triples = main._read_kg_postgres()
        self.assertEqual(entities, [])
        self.assertEqual(triples, [])


if __name__ == "__main__":
    unittest.main()
