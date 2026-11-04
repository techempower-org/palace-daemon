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


if __name__ == "__main__":
    unittest.main()
