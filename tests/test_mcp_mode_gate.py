"""Tests for the mcp_mode CLI-only gate in the stdio proxy (issue #58).

The proxy (``clients/mempalace-mcp.py``) can suppress its MCP tool surface
per-machine so the palace runs CLI-only — hooks and skills keep working, but
``tools/list`` advertises zero tools (reclaiming ~9k tokens of schema context)
and ``tools/call`` is rejected. Mode resolution is fail-open: env override beats
the config file, and any unknown/missing/garbled value falls back to "all".

The proxy module has a hyphen in its filename so it can't be imported by name;
we load it via importlib.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mcp_mode_gate.py -q
"""
import importlib.util
import json
import os
import unittest
import unittest.mock
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROXY_PATH = os.path.join(os.path.dirname(_HERE), "clients", "mempalace-mcp.py")

_spec = importlib.util.spec_from_file_location("mempalace_mcp_proxy", _PROXY_PATH)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


def _build_handle(mcp_mode):
    """Build the handle() closure for a given mode by intercepting the stdio
    loop. run_daemon_mode wires handle() into _stdio_loop; we grab it instead
    of running the loop."""
    grabbed = {}
    with patch.object(proxy, "_stdio_loop", lambda handle: grabbed.setdefault("handle", handle)):
        proxy.run_daemon_mode("http://daemon", mcp_mode)
    return grabbed["handle"]


def _run(handle, request):
    """Invoke handle with proxy.forward patched to record + echo a marker, so
    the patch is active while handle() runs (forward is resolved at call time)."""
    captured = []

    def _forward(_url, req):
        captured.append(req)
        return {"jsonrpc": "2.0", "id": req.get("id"), "result": {"forwarded": True}}

    with patch.object(proxy, "forward", _forward):
        resp = handle(request)
    return resp, captured


class TestResolveMcpMode(unittest.TestCase):
    def test_env_override_beats_config(self):
        with patch.dict(os.environ, {"PALACE_MCP_MODE": "cli-only"}), \
             patch.object(proxy, "CONFIG_PATH", "/nonexistent"):
            self.assertEqual(proxy.resolve_mcp_mode(), "cli-only")

    def test_env_unknown_value_falls_back_to_all(self):
        with patch.dict(os.environ, {"PALACE_MCP_MODE": "bogus"}):
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_config_cli_only_is_honored(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"mcp_mode": "cli-only"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            os.environ.pop("PALACE_MCP_MODE", None)
            self.assertEqual(proxy.resolve_mcp_mode(), "cli-only")

    def test_config_unknown_value_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"mcp_mode": "typo"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            os.environ.pop("PALACE_MCP_MODE", None)
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_missing_config_falls_back_to_all(self):
        with patch.object(proxy, "CONFIG_PATH", "/definitely/not/here.json"):
            os.environ.pop("PALACE_MCP_MODE", None)
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_garbled_config_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data="{not valid json")
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            os.environ.pop("PALACE_MCP_MODE", None)
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_missing_key_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"collection_name": "x"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            os.environ.pop("PALACE_MCP_MODE", None)
            self.assertEqual(proxy.resolve_mcp_mode(), "all")


class TestCliOnlyGate(unittest.TestCase):
    def test_tools_list_returns_empty_without_forwarding(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(resp, {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
        self.assertEqual(captured, [], "tools/list must not be forwarded in cli-only")

    def test_tools_call_rejected_with_32601(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                       "params": {"name": "mempalace_search", "arguments": {}}})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertIn("cli-only", resp["error"]["message"])
        self.assertEqual(captured, [], "tools/call must not be forwarded in cli-only")

    def test_initialize_still_forwards_in_cli_only(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 3, "method": "initialize"})
        self.assertEqual(resp["result"], {"forwarded": True})
        self.assertEqual(len(captured), 1)

    def test_ping_and_resources_still_forward_in_cli_only(self):
        handle = _build_handle("cli-only")
        seen = []
        for method in ("ping", "resources/list", "prompts/list"):
            _, captured = _run(handle, {"jsonrpc": "2.0", "id": 9, "method": method})
            seen.extend(r["method"] for r in captured)
        self.assertEqual(seen, ["ping", "resources/list", "prompts/list"])


class TestAllModePassthrough(unittest.TestCase):
    def test_tools_list_forwards_in_all_mode(self):
        handle = _build_handle("all")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 4, "method": "tools/list"})
        self.assertEqual(resp["result"], {"forwarded": True})
        self.assertEqual(len(captured), 1)

    def test_tools_call_forwards_in_all_mode(self):
        handle = _build_handle("all")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                       "params": {"name": "mempalace_search", "arguments": {}}})
        self.assertEqual(resp["result"], {"forwarded": True})
        self.assertEqual(len(captured), 1)


if __name__ == "__main__":
    unittest.main()
