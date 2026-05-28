"""Tests for daemon-native MCP tools (issue #93).

Six tools route the mempalace CLI's rooms/wake-up/mined commands through
the daemon's authoritative postgres instead of opening a local-palace
client. None of them proxy to upstream mempalace — they're implemented
entirely in palace-daemon.

What's locked in here:

* Each tool registers in ``_DAEMON_NATIVE_MCP_TOOLS`` so /mcp dispatches
  to it before falling through to the upstream MCP path.
* Happy-path payloads match the contract documented in issue #93.
* Validation errors surface as JSON-RPC ``-32602`` invalid-params
  envelopes (via the proxy's ``_DaemonToolError`` translation).
* Backend-down (no postgres DSN) surfaces as ``-32004`` so callers can
  distinguish "wrong configuration" from "bad input".
* ``mempalace_rooms_{add,rename,remove}`` invalidate the daemon's
  canonical-rooms cache inline — the next /memory write sees the new
  set without a separate /admin/refresh-rooms call.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m unittest tests.test_mcp_daemon_native_tools -v
"""
import datetime as _dt
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


# ── Test helpers ──────────────────────────────────────────────────────────────


class _FakeCursor:
    """Minimal psycopg2 cursor stub with a scripted response queue.

    Each test pushes (rows, rowcount) tuples; the handler's `execute()`
    pops the next one and exposes it via fetchall/fetchone/rowcount.
    """

    def __init__(self, script):
        self._script = list(script)
        self._rows: list = []
        self.rowcount = 0
        self.executed: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        # SET LOCAL statement_timeout doesn't consume a scripted entry.
        if "SET LOCAL" in sql:
            return
        if not self._script:
            self._rows = []
            self.rowcount = 0
            return
        next_step = self._script.pop(0)
        if isinstance(next_step, Exception):
            raise next_step
        rows, rowcount = next_step
        self._rows = list(rows)
        self.rowcount = rowcount

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    def __init__(self, cursor):
        self._cursor = cursor
        self.autocommit = False
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self._cursor

    def close(self):
        self.closed = True


def _patch_psycopg(script):
    """Make every ``_connect_postgres`` in this test return a scripted conn.

    The conn's cursor walks ``script`` (a list of (rows, rowcount) tuples
    or Exceptions) in order on each .execute() that isn't a SET LOCAL.
    """
    cursor = _FakeCursor(script)
    conn = _FakeConn(cursor)

    def fake_connect_postgres(tool, autocommit=False):
        conn.autocommit = autocommit
        return conn

    return patch.object(main, "_connect_postgres", side_effect=fake_connect_postgres), conn, cursor


# ── Dispatch table & error envelope contract ─────────────────────────────────


class TestDispatchTable(unittest.TestCase):
    """The six tools must be registered and live in the proxy's intercept
    set so the upstream MCP path can never see them by accident."""

    def test_six_tools_registered(self):
        self.assertEqual(
            set(main._DAEMON_NATIVE_MCP_TOOLS.keys()),
            {
                "mempalace_wakeup",
                "mempalace_mined",
                "mempalace_rooms_list",
                "mempalace_rooms_add",
                "mempalace_rooms_rename",
                "mempalace_rooms_remove",
            },
        )

    def test_all_handlers_are_callable(self):
        for name, fn in main._DAEMON_NATIVE_MCP_TOOLS.items():
            self.assertTrue(callable(fn), f"{name} handler not callable")


class TestProxyErrorEnvelopes(unittest.IsolatedAsyncioTestCase):
    """``/mcp`` must surface daemon-native failures as JSON-RPC errors
    rather than 500s — the CLI consumer parses error.code to distinguish
    bad input from "daemon doesn't support this yet"."""

    def _make_request(self, body: dict):
        class _R:
            async def json(self_inner):
                return body
        return _R()

    async def _call_proxy(self, body):
        resp = await main.mcp_proxy(self._make_request(body), x_api_key=None)
        return json.loads(resp.body)

    async def test_validation_error_maps_to_invalid_params(self):
        body = {
            "jsonrpc": "2.0", "id": 99, "method": "tools/call",
            "params": {"name": "mempalace_rooms_add",
                       "arguments": {"name": "Has SPACES"}},
        }
        with patch.object(main, "_check_auth"):
            envelope = await self._call_proxy(body)
        self.assertEqual(envelope["jsonrpc"], "2.0")
        self.assertEqual(envelope["id"], 99)
        self.assertEqual(envelope["error"]["code"], -32602)
        self.assertIn("snake_case", envelope["error"]["message"])

    async def test_no_postgres_dsn_maps_to_backend_down(self):
        body = {
            "jsonrpc": "2.0", "id": 100, "method": "tools/call",
            "params": {"name": "mempalace_rooms_list", "arguments": {}},
        }
        with patch.object(main, "_check_auth"), \
             patch.object(main, "_postgres_dsn", return_value=None):
            envelope = await self._call_proxy(body)
        self.assertEqual(envelope["error"]["code"], -32004)
        self.assertIn("postgres", envelope["error"]["message"].lower())

    async def test_unexpected_exception_maps_to_internal_error(self):
        # When a handler raises something other than _DaemonToolError —
        # e.g., a psycopg2 OperationalError — the proxy must wrap it in
        # -32000 so the CLI doesn't get a JSON-RPC envelope that lacks
        # both `result` and a recognized error code.
        #
        # The dispatch table holds direct function references (captured
        # at module-import time), so patching ``main._fast_mcp_rooms_list_payload``
        # would miss the proxy's lookup. Patch the dict entry directly.
        body = {
            "jsonrpc": "2.0", "id": 101, "method": "tools/call",
            "params": {"name": "mempalace_rooms_list", "arguments": {}},
        }

        def _boom(_args):
            raise RuntimeError("kaboom")

        with patch.object(main, "_check_auth"), \
             patch.dict(main._DAEMON_NATIVE_MCP_TOOLS,
                        {"mempalace_rooms_list": _boom}):
            envelope = await self._call_proxy(body)
        self.assertEqual(envelope["error"]["code"], -32000)
        self.assertIn("kaboom", envelope["error"]["message"])

    async def test_other_tools_still_proxy_to_call(self):
        # Sanity: a non-daemon-native tool must keep going through _call
        # so the upstream MCP path stays intact.
        body = {
            "jsonrpc": "2.0", "id": 102, "method": "tools/call",
            "params": {"name": "mempalace_search", "arguments": {"query": "x"}},
        }

        async def _slow(_b):
            return {"jsonrpc": "2.0", "id": 102, "result": {"hits": []}}

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=_slow) as slow:
            envelope = await self._call_proxy(body)
        slow.assert_called_once()
        self.assertEqual(envelope["result"], {"hits": []})


# ── mempalace_wakeup ─────────────────────────────────────────────────────────


class TestWakeup(unittest.TestCase):
    """L0 identity + L1 essential-story rendered from postgres."""

    def test_happy_path_with_identity_and_drawers(self):
        # L1 fetch returns three high-importance drawers under two rooms.
        rows = [
            (5.0, {"room": "decisions", "source_file": "/p/a.md"}, "decided to ship v2"),
            (4.5, {"room": "decisions", "source_file": "/p/b.md"}, "rolled back v1"),
            (4.0, {"room": "sessions", "source_file": "/p/c.md"}, "started a new session"),
        ]
        script = [(rows, 3)]
        ctx, conn, cur = _patch_psycopg(script)
        with ctx, \
             patch("os.path.exists", return_value=True), \
             patch("builtins.open", unittest.mock.mock_open(read_data="I am Atlas.")):
            payload = main._fast_mcp_wakeup_payload({})

        self.assertIn("I am Atlas.", payload["text"])
        self.assertIn("## L1 — ESSENTIAL STORY", payload["text"])
        self.assertIn("[decisions]", payload["text"])
        self.assertIn("[sessions]", payload["text"])
        self.assertIn("decided to ship v2", payload["text"])
        self.assertIsInstance(payload["tokens"], int)
        self.assertGreater(payload["tokens"], 0)

    def test_wing_filter_passes_through_to_sql(self):
        ctx, conn, cur = _patch_psycopg([( [], 0 ), ( [], 0 )])
        with ctx, \
             patch("os.path.exists", return_value=False):
            main._fast_mcp_wakeup_payload({"wing": "mempalace"})

        # Both the fast path and the fallback should include `wing = %s`.
        wing_calls = [args for sql, args in cur.executed
                      if isinstance(sql, str) and "wing = %s" in sql]
        self.assertTrue(wing_calls, "wing filter must be pushed to SQL")
        # All wing-filter calls carry the wing value as the first param.
        for params in wing_calls:
            self.assertEqual(params[0], "mempalace")

    def test_wing_must_be_string(self):
        with self.assertRaises(main._DaemonToolError):
            main._fast_mcp_wakeup_payload({"wing": 42})

    def test_no_identity_no_drawers_still_renders(self):
        ctx, conn, cur = _patch_psycopg([( [], 0 ), ( [], 0 )])
        with ctx, patch("os.path.exists", return_value=False):
            payload = main._fast_mcp_wakeup_payload({})
        self.assertIn("L0 — IDENTITY", payload["text"])
        self.assertIn("L1 — No memories yet.", payload["text"])


# ── mempalace_mined ──────────────────────────────────────────────────────────


class TestMined(unittest.TestCase):
    """Group source_file metadata by wing — direct SQL aggregation."""

    def test_happy_path_shape_matches_cli_json(self):
        # GROUP BY wing, source_file produces (wing, source_file, count) rows.
        rows = [
            ("wing_a", "/p/x.md", 12),
            ("wing_a", "/p/y.md", 3),
            ("wing_b", "/p/z.md", 7),
        ]
        ctx, conn, cur = _patch_psycopg([(rows, len(rows))])
        with ctx:
            payload = main._fast_mcp_mined_payload({})

        self.assertEqual(payload["wing_filter"], None)
        self.assertEqual(payload["limit"], 0)
        self.assertEqual(payload["total_wings"], 2)
        self.assertEqual(payload["total_sources"], 3)

        wing_a = payload["sources_by_wing"]["wing_a"]
        self.assertEqual(wing_a["total_sources"], 2)
        self.assertEqual(wing_a["total_drawers"], 15)
        self.assertFalse(wing_a["truncated"])
        # Ordered by drawer_count desc.
        self.assertEqual(wing_a["sources"][0]["source_file"], "/p/x.md")
        self.assertEqual(wing_a["sources"][0]["drawer_count"], 12)

    def test_limit_truncates_per_wing(self):
        rows = [
            ("wing_a", f"/p/{i}.md", 100 - i) for i in range(5)
        ]
        ctx, conn, cur = _patch_psycopg([(rows, len(rows))])
        with ctx:
            payload = main._fast_mcp_mined_payload({"limit": 2})
        wing_a = payload["sources_by_wing"]["wing_a"]
        self.assertEqual(len(wing_a["sources"]), 2)
        self.assertEqual(wing_a["total_sources"], 5)
        self.assertTrue(wing_a["truncated"])

    def test_wing_filter_threads_into_sql(self):
        ctx, conn, cur = _patch_psycopg([([], 0)])
        with ctx:
            main._fast_mcp_mined_payload({"wing": "wing_a"})
        # The GROUP BY query must carry an `AND wing = %s` clause.
        group_calls = [
            (sql, args) for sql, args in cur.executed if "GROUP BY" in (sql or "")
        ]
        self.assertTrue(group_calls)
        sql, args = group_calls[0]
        self.assertIn("wing = %s", sql)
        self.assertEqual(args[-1], "wing_a")

    def test_invalid_limit_rejected(self):
        for bad in (-1, "5", 1.5, True):
            with self.assertRaises(main._DaemonToolError):
                main._fast_mcp_mined_payload({"limit": bad})


# ── mempalace_rooms_list ──────────────────────────────────────────────────────


class TestRoomsList(unittest.TestCase):

    def test_happy_path_shape(self):
        added_at = _dt.datetime(2026, 1, 1, 12, 0, 0)
        rows = [
            ("decisions", "where decisions live", added_at),
            ("problems", "where problems live", added_at),
        ]
        ctx, conn, cur = _patch_psycopg([(rows, len(rows))])
        with ctx:
            payload = main._fast_mcp_rooms_list_payload({})
        self.assertEqual(len(payload), 2)
        self.assertEqual(payload[0]["name"], "decisions")
        self.assertEqual(payload[0]["description"], "where decisions live")
        self.assertEqual(payload[0]["added_at"], added_at.isoformat())

    def test_returns_empty_when_table_absent(self):
        # An UndefinedTable-shaped exception should resolve to [], not raise.
        err = Exception('relation "mempalace_canonical_rooms" does not exist')
        ctx, conn, cur = _patch_psycopg([err])
        with ctx:
            payload = main._fast_mcp_rooms_list_payload({})
        self.assertEqual(payload, [])

    def test_extra_args_ignored(self):
        # MCP convention: don't 400 on stray keys.
        ctx, conn, cur = _patch_psycopg([([], 0)])
        with ctx:
            payload = main._fast_mcp_rooms_list_payload({"unknown": "thing"})
        self.assertEqual(payload, [])


# ── mempalace_rooms_add ──────────────────────────────────────────────────────


class TestRoomsAdd(unittest.TestCase):

    def setUp(self):
        # Reset cache so we can assert invalidation.
        main._canonical_rooms_cache = {"sentinel"}

    def tearDown(self):
        main._canonical_rooms_cache = None

    def test_inserted_returns_added(self):
        # xmax = 0 → ROW WAS INSERTED.
        ctx, conn, cur = _patch_psycopg([([(True,)], 1)])
        with ctx:
            payload = main._fast_mcp_rooms_add_payload(
                {"name": "ideas", "description": "scratch space"}
            )
        self.assertEqual(payload, {"action": "added", "name": "ideas"})
        self.assertIsNone(main._canonical_rooms_cache,
                          "cache must be invalidated inline")

    def test_conflict_returns_updated(self):
        ctx, conn, cur = _patch_psycopg([([(False,)], 1)])
        with ctx:
            payload = main._fast_mcp_rooms_add_payload(
                {"name": "ideas", "description": "now with more space"}
            )
        self.assertEqual(payload, {"action": "updated", "name": "ideas"})

    def test_name_validation(self):
        for bad in (None, "", "Has Space", "kebab-case", "nope!", 42):
            with self.assertRaises(main._DaemonToolError):
                main._fast_mcp_rooms_add_payload({"name": bad})

    def test_description_must_be_string(self):
        with self.assertRaises(main._DaemonToolError):
            main._fast_mcp_rooms_add_payload({"name": "ok", "description": 5})


# ── mempalace_rooms_rename ────────────────────────────────────────────────────


class TestRoomsRename(unittest.TestCase):

    def setUp(self):
        main._canonical_rooms_cache = {"sentinel"}

    def tearDown(self):
        main._canonical_rooms_cache = None

    def test_happy_path(self):
        # UPDATE rowcount = 1; SELECT count(*) returns the affected drawer count.
        script = [
            ([], 1),                # UPDATE
            ([(42,)], 1),           # SELECT count(*)
        ]
        ctx, conn, cur = _patch_psycopg(script)
        with ctx:
            payload = main._fast_mcp_rooms_rename_payload(
                {"old": "thoughts", "new": "ideas"}
            )
        self.assertEqual(
            payload, {"old": "thoughts", "new": "ideas", "affected_drawers": 42}
        )
        self.assertIsNone(main._canonical_rooms_cache)

    def test_missing_source_room(self):
        script = [([], 0)]  # UPDATE rowcount = 0
        ctx, conn, cur = _patch_psycopg(script)
        with ctx:
            with self.assertRaises(main._DaemonToolError) as ec:
                main._fast_mcp_rooms_rename_payload(
                    {"old": "ghost", "new": "ideas"}
                )
        self.assertIn("ghost", str(ec.exception))

    def test_validation_blocks_bad_new_name(self):
        with self.assertRaises(main._DaemonToolError):
            main._fast_mcp_rooms_rename_payload(
                {"old": "ideas", "new": "Bad Name"}
            )


# ── mempalace_rooms_remove ────────────────────────────────────────────────────


class TestRoomsRemove(unittest.TestCase):

    def setUp(self):
        main._canonical_rooms_cache = {"sentinel"}

    def tearDown(self):
        main._canonical_rooms_cache = None

    def test_happy_path(self):
        script = [
            ([(0,)], 1),  # SELECT count(*) → 0 drawers
            ([], 1),      # DELETE rowcount = 1
        ]
        ctx, conn, cur = _patch_psycopg(script)
        with ctx:
            payload = main._fast_mcp_rooms_remove_payload({"name": "ideas"})
        self.assertEqual(payload, {"name": "ideas", "removed": True})
        self.assertIsNone(main._canonical_rooms_cache)

    def test_refuses_when_drawers_still_reference_room(self):
        script = [([(3,)], 1)]  # 3 drawers still point here
        ctx, conn, cur = _patch_psycopg(script)
        with ctx:
            with self.assertRaises(main._DaemonToolError) as ec:
                main._fast_mcp_rooms_remove_payload({"name": "ideas"})
        msg = str(ec.exception)
        self.assertIn("ideas", msg)
        self.assertIn("3", msg)

    def test_missing_room(self):
        script = [
            ([(0,)], 1),  # SELECT count(*) → 0
            ([], 0),      # DELETE rowcount = 0
        ]
        ctx, conn, cur = _patch_psycopg(script)
        with ctx:
            with self.assertRaises(main._DaemonToolError) as ec:
                main._fast_mcp_rooms_remove_payload({"name": "ghost"})
        self.assertIn("ghost", str(ec.exception))


if __name__ == "__main__":
    unittest.main()
