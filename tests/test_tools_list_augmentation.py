"""Tests for /mcp tools/list augmentation (palace-daemon#140).

The upstream mempalace MCP server's tools/list doesn't know about the
6 daemon-native tools registered in daemon_tools.DAEMON_NATIVE_TOOLS,
so MCP clients that use the standard discovery handshake can't find
them. /mcp's mcp_proxy intercepts tools/list, forwards to upstream,
then merges in the daemon-native descriptors before returning.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_tools_list_augmentation.py -v
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
import daemon_tools  # noqa: E402


class TestToolsListAugmentation(unittest.IsolatedAsyncioTestCase):
    """tools/list response must include the 6 daemon-native tools."""

    def _make_request(self, body: dict):
        class _R:
            async def json(_self_inner):
                return body
        return _R()

    async def _call_proxy(self, body):
        resp = await main.mcp_proxy(self._make_request(body), x_api_key=None)
        return json.loads(resp.body)

    async def test_augments_tools_list_with_daemon_native_descriptors(self):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        upstream_tools = [
            {"name": "mempalace_status", "description": "...", "inputSchema": {"type": "object", "properties": {}}},
            {"name": "mempalace_search", "description": "...", "inputSchema": {"type": "object", "properties": {}}},
        ]

        async def _fake_call(_body):
            return {
                "jsonrpc": "2.0", "id": 1,
                "result": {"tools": list(upstream_tools)},
            }

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=_fake_call):
            envelope = await self._call_proxy(body)

        tools = envelope["result"]["tools"]
        names = {t["name"] for t in tools}
        # Upstream tools preserved.
        self.assertIn("mempalace_status", names)
        self.assertIn("mempalace_search", names)
        # All 6 daemon-native tools present.
        for expected in (
            "mempalace_rooms_list",
            "mempalace_rooms_add",
            "mempalace_rooms_rename",
            "mempalace_rooms_remove",
            "mempalace_mined",
            "mempalace_wakeup",
        ):
            self.assertIn(expected, names, f"daemon-native {expected!r} should be in tools/list")
        self.assertEqual(len(tools), len(upstream_tools) + 6)

    async def test_descriptor_shape_matches_mcp_spec(self):
        """Each descriptor must have name, description, and inputSchema."""
        for descriptor in daemon_tools.DAEMON_NATIVE_TOOL_DESCRIPTORS:
            self.assertIn("name", descriptor)
            self.assertIn("description", descriptor)
            self.assertIn("inputSchema", descriptor)
            self.assertEqual(descriptor["inputSchema"].get("type"), "object")
            # MCP spec requires properties on object schemas — even empty {}.
            self.assertIn("properties", descriptor["inputSchema"])

    async def test_descriptors_cover_every_dispatch_entry(self):
        """Every name in DAEMON_NATIVE_TOOLS must have a descriptor and vice versa."""
        dispatch_names = set(daemon_tools.DAEMON_NATIVE_TOOLS.keys())
        descriptor_names = {d["name"] for d in daemon_tools.DAEMON_NATIVE_TOOL_DESCRIPTORS}
        self.assertEqual(
            dispatch_names, descriptor_names,
            f"dispatch table and descriptor list must agree — "
            f"missing descriptor for: {dispatch_names - descriptor_names}, "
            f"missing dispatch for: {descriptor_names - dispatch_names}",
        )

    async def test_skips_duplicate_if_upstream_already_lists_it(self):
        """Defensive: if mempalace ever adds the same name, don't double up."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

        # Upstream already lists one of our descriptors' names.
        upstream_tools = [
            {"name": "mempalace_rooms_list", "description": "(upstream)", "inputSchema": {"type": "object", "properties": {}}},
        ]

        async def _fake_call(_body):
            return {"jsonrpc": "2.0", "id": 1, "result": {"tools": list(upstream_tools)}}

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=_fake_call):
            envelope = await self._call_proxy(body)

        tools = envelope["result"]["tools"]
        names = [t["name"] for t in tools]
        # mempalace_rooms_list appears exactly once (from upstream), the
        # other 5 daemon-native tools are appended.
        self.assertEqual(names.count("mempalace_rooms_list"), 1)
        self.assertEqual(len(tools), 1 + 5)

    async def test_other_methods_unaffected(self):
        """Methods other than tools/list pass through unchanged."""
        body = {"jsonrpc": "2.0", "id": 2, "method": "ping", "params": {}}

        async def _fake_call(_body):
            return {"jsonrpc": "2.0", "id": 2, "result": {"pong": True}}

        with patch.object(main, "_check_auth"), \
             patch.object(main, "_call", side_effect=_fake_call):
            envelope = await self._call_proxy(body)

        self.assertEqual(envelope["result"], {"pong": True})


if __name__ == "__main__":
    unittest.main()
