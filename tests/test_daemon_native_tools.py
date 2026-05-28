"""Tests for the daemon-native MCP tools (palace-daemon#93).

These six tools replace the local-ChromaDB-opening CLI commands in mempalace
(`cmd_rooms`, `cmd_wakeup`, `cmd_mined`) — the CLI breaks under daemon-strict
mode because the local palace is retired. The daemon-side implementation
hangs off the existing `/mcp` fast-intercept dispatch.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_daemon_native_tools.py -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class _FakeCursor:
    """Mock psycopg2 cursor: scripted execute → fetchone/fetchall, rowcount."""

    def __init__(self, script):
        self._script = list(script)  # list of (sql_substr, response, [rowcount])
        self._next = None
        self.rowcount = 0
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # SET LOCAL statement_timeout is always allowed without scripting
        if "statement_timeout" in sql:
            return
        for i, entry in enumerate(self._script):
            substr, resp = entry[0], entry[1]
            rowcount = entry[2] if len(entry) > 2 else None
            if substr in sql:
                self._next = resp
                self.rowcount = rowcount if rowcount is not None else (
                    len(resp) if isinstance(resp, list) else 1
                )
                self._script.pop(i)
                return
        # Unscripted SQL → next is empty
        self._next = None
        self.rowcount = 0

    def fetchone(self):
        if isinstance(self._next, list):
            return self._next[0] if self._next else None
        return self._next

    def fetchall(self):
        if isinstance(self._next, list):
            return self._next
        return [self._next] if self._next is not None else []


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        pass


def _patch_psycopg(cursor, error_to_raise=None):
    """Build a patch for psycopg2.connect that returns our fake conn."""
    if error_to_raise is not None:
        return patch.object(main, "_postgres_dsn", return_value="postgres://x")
    return [
        patch.object(main, "_postgres_dsn", return_value="postgres://x"),
        patch("psycopg2.connect", return_value=_FakeConn(cursor)),
    ]


class _BaseTool(unittest.TestCase):
    def setUp(self):
        # Reset the rooms cache between tests so invalidation behavior is
        # observable.
        main._canonical_rooms_cache = None


class TestRequirePostgres(_BaseTool):
    def test_missing_dsn_raises_backend_down(self):
        with patch.object(main, "_postgres_dsn", return_value=None):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._require_postgres()
        self.assertEqual(cm.exception.code, main._RPC_BACKEND_DOWN)


class TestRoomsList(_BaseTool):
    def test_returns_rows(self):
        rows = [("planning", "Planning room", "2026-01-01")]
        cur = _FakeCursor([("FROM mempalace_canonical_rooms", rows)])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_list({})
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "planning")
        self.assertEqual(result[0]["description"], "Planning room")

    def test_undefined_table_returns_empty_list(self):
        from psycopg2 import errors as pg_errors

        class _UndefinedCursor(_FakeCursor):
            def execute(self, sql, params=None):
                if "FROM mempalace_canonical_rooms" in sql:
                    raise pg_errors.UndefinedTable("relation does not exist")
                super().execute(sql, params)

        cur = _UndefinedCursor([])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_list({})
        self.assertEqual(result, [])

    def test_backend_down(self):
        with patch.object(main, "_postgres_dsn", return_value=None):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._fast_mcp_rooms_list({})
        self.assertEqual(cm.exception.code, main._RPC_BACKEND_DOWN)


class TestRoomsAdd(_BaseTool):
    def test_add_inserts_returns_added(self):
        cur = _FakeCursor([("INSERT INTO mempalace_canonical_rooms", [(True,)])])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_add({"name": "Planning", "description": "Planning room"})
        self.assertEqual(result, {"action": "added", "name": "planning"})

    def test_add_existing_returns_updated(self):
        cur = _FakeCursor([("INSERT INTO mempalace_canonical_rooms", [(False,)])])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_add({"name": "planning"})
        self.assertEqual(result, {"action": "updated", "name": "planning"})

    def test_add_invalidates_cache(self):
        main._canonical_rooms_cache = {"existing"}
        cur = _FakeCursor([("INSERT", [(True,)])])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            main._fast_mcp_rooms_add({"name": "new"})
        self.assertIsNone(main._canonical_rooms_cache)

    def test_blank_name_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_rooms_add({"name": "  "})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_non_string_name_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_rooms_add({"name": 42})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_non_string_description_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_rooms_add({"name": "x", "description": 42})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_normalizes_name(self):
        cur = _FakeCursor([("INSERT", [(True,)])])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_add({"name": "  PLANNING  "})
        self.assertEqual(result["name"], "planning")


class TestRoomsRename(_BaseTool):
    def test_rename_returns_affected_drawers(self):
        cur = _FakeCursor([
            ("count(*) FROM mempalace_drawers", [(7,)], 1),
            ("UPDATE mempalace_canonical_rooms", None, 1),
        ])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_rename({"old": "Planning", "new": "Plans"})
        self.assertEqual(result, {"old": "planning", "new": "plans", "affected_drawers": 7})

    def test_rename_missing_room_raises_invalid_params(self):
        cur = _FakeCursor([
            ("count(*) FROM mempalace_drawers", [(0,)], 1),
            ("UPDATE mempalace_canonical_rooms", None, 0),
        ])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._fast_mcp_rooms_rename({"old": "ghost", "new": "spirit"})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)
        self.assertIn("does not exist", str(cm.exception))

    def test_rename_collision_raises_invalid_params(self):
        from psycopg2 import errors as pg_errors

        class _CollideCursor(_FakeCursor):
            def execute(self, sql, params=None):
                if "UPDATE mempalace_canonical_rooms" in sql:
                    raise pg_errors.UniqueViolation("already exists")
                super().execute(sql, params)

        cur = _CollideCursor([("count(*) FROM mempalace_drawers", [(3,)], 1)])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._fast_mcp_rooms_rename({"old": "a", "new": "b"})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)
        self.assertIn("already exists", str(cm.exception))

    def test_same_name_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_rooms_rename({"old": "X", "new": "x"})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)


class TestRoomsRemove(_BaseTool):
    def test_remove_with_no_references_succeeds(self):
        cur = _FakeCursor([
            ("count(*) FROM mempalace_drawers", [(0,)], 1),
            ("DELETE FROM mempalace_canonical_rooms", None, 1),
        ])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_remove({"name": "old_room"})
        self.assertEqual(result, {"name": "old_room", "removed": True})

    def test_remove_refused_when_referenced(self):
        cur = _FakeCursor([("count(*) FROM mempalace_drawers", [(5,)], 1)])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._fast_mcp_rooms_remove({"name": "used"})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)
        self.assertIn("5 drawers", str(cm.exception))
        # Structured data carries the count for CLI consumers
        self.assertEqual(cm.exception.data["referencing_drawers"], 5)

    def test_remove_nonexistent_returns_removed_false(self):
        cur = _FakeCursor([
            ("count(*) FROM mempalace_drawers", [(0,)], 1),
            ("DELETE FROM mempalace_canonical_rooms", None, 0),
        ])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_rooms_remove({"name": "ghost"})
        self.assertEqual(result, {"name": "ghost", "removed": False})


class TestMined(_BaseTool):
    def test_groups_by_wing(self):
        rows = [
            ("project_a", "/path/a.txt", 12),
            ("project_a", "/path/b.txt", 3),
            ("project_b", "/path/c.txt", 7),
        ]
        cur = _FakeCursor([("FROM mempalace_drawers", rows)])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_mined({})
        self.assertEqual(result["total_wings"], 2)
        self.assertEqual(result["total_sources"], 3)
        a = result["sources_by_wing"]["project_a"]
        self.assertEqual(a["total_drawers"], 15)
        self.assertEqual(a["total_sources"], 2)
        self.assertFalse(a["truncated"])

    def test_wing_filter_passed_to_sql(self):
        cur = _FakeCursor([("FROM mempalace_drawers", [("project_a", "/x", 1)])])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            main._fast_mcp_mined({"wing": "project_a"})
        # The executed SQL must include the WHERE wing = %s clause
        wing_sql = [e for e in cur.executed if "AND wing = %s" in e[0]]
        self.assertEqual(len(wing_sql), 1)
        self.assertEqual(wing_sql[0][1], ["project_a"])

    def test_limit_truncates_per_wing(self):
        rows = [("w", f"/file{i}.txt", 1) for i in range(5)]
        cur = _FakeCursor([("FROM mempalace_drawers", rows)])
        with patch.object(main, "_postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            result = main._fast_mcp_mined({"limit": 2})
        slot = result["sources_by_wing"]["w"]
        self.assertEqual(len(slot["sources"]), 2)
        self.assertEqual(slot["total_sources"], 5)
        self.assertTrue(slot["truncated"])

    def test_non_int_limit_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_mined({"limit": "ten"})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_zero_limit_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_mined({"limit": 0})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_non_string_wing_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_mined({"wing": 42})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)


class TestWakeup(_BaseTool):
    def test_returns_text_and_tokens(self):
        fake_stack = MagicMock()
        fake_stack.wake_up.return_value = "x" * 400
        fake_module = MagicMock()
        fake_module.MemoryStack.return_value = fake_stack
        with patch.dict(sys.modules, {"mempalace.layers": fake_module}):
            result = main._fast_mcp_wakeup({"wing": "myproj"})
        self.assertEqual(result["text"], "x" * 400)
        self.assertEqual(result["tokens"], 100)  # 400 // 4
        self.assertEqual(result["wing"], "myproj")
        fake_stack.wake_up.assert_called_once_with(wing="myproj")

    def test_no_wing_passes_none(self):
        fake_stack = MagicMock()
        fake_stack.wake_up.return_value = "hello"
        fake_module = MagicMock()
        fake_module.MemoryStack.return_value = fake_stack
        with patch.dict(sys.modules, {"mempalace.layers": fake_module}):
            result = main._fast_mcp_wakeup({})
        self.assertIsNone(result["wing"])
        fake_stack.wake_up.assert_called_once_with(wing=None)

    def test_non_string_wing_raises_invalid_params(self):
        with self.assertRaises(main._DaemonToolError) as cm:
            main._fast_mcp_wakeup({"wing": 42})
        self.assertEqual(cm.exception.code, main._RPC_INVALID_PARAMS)

    def test_memorystack_failure_raises_internal(self):
        fake_module = MagicMock()
        fake_module.MemoryStack.side_effect = RuntimeError("backend down")
        with patch.dict(sys.modules, {"mempalace.layers": fake_module}):
            with self.assertRaises(main._DaemonToolError) as cm:
                main._fast_mcp_wakeup({})
        self.assertEqual(cm.exception.code, main._RPC_INTERNAL)
        self.assertIn("backend down", str(cm.exception))


class TestDispatchTable(_BaseTool):
    """The six tools are all registered."""

    def test_all_six_in_dispatch(self):
        expected = {
            "mempalace_rooms_list",
            "mempalace_rooms_add",
            "mempalace_rooms_rename",
            "mempalace_rooms_remove",
            "mempalace_mined",
            "mempalace_wakeup",
        }
        self.assertEqual(set(main._DAEMON_NATIVE_TOOLS), expected)


if __name__ == "__main__":
    unittest.main()
