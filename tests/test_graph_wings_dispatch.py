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
    def test_postgres_backend_returns_empty_not_stale_sqlite(self):
        """Under postgres backend the KG lives in AGE; the sibling
        knowledge_graph.sqlite3 — if present — is a pre-migration
        leftover. Returning its contents would surface frozen snapshot
        data, so we short-circuit to empty.
        """
        with patch.object(main, "_mp") as mp, \
             patch("sqlite3.connect") as sql_connect:
            mp._config = _Cfg("postgres")
            entities, triples = main._read_kg_direct()
        sql_connect.assert_not_called()
        self.assertEqual(entities, [])
        self.assertEqual(triples, [])


if __name__ == "__main__":
    unittest.main()
