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
    def setUp(self):
        # Isolate os.environ per test: patch.dict restores the original
        # environment (including any pre-existing PALACE_MCP_MODE) on cleanup,
        # so popping it below can't leak into other tests.
        patcher = patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("PALACE_MCP_MODE", None)

    def test_env_override_beats_config(self):
        os.environ["PALACE_MCP_MODE"] = "cli-only"
        with patch.object(proxy, "CONFIG_PATH", "/nonexistent"):
            self.assertEqual(proxy.resolve_mcp_mode(), "cli-only")

    def test_env_unknown_value_falls_back_to_all(self):
        os.environ["PALACE_MCP_MODE"] = "bogus"
        self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_config_cli_only_is_honored(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"mcp_mode": "cli-only"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            self.assertEqual(proxy.resolve_mcp_mode(), "cli-only")

    def test_config_unknown_value_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"mcp_mode": "typo"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_missing_config_falls_back_to_all(self):
        with patch.object(proxy, "CONFIG_PATH", "/definitely/not/here.json"):
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_garbled_config_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data="{not valid json")
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
            self.assertEqual(proxy.resolve_mcp_mode(), "all")

    def test_missing_key_falls_back_to_all(self):
        m = unittest.mock.mock_open(read_data=json.dumps({"collection_name": "x"}))
        with patch.object(proxy, "CONFIG_PATH", "/cfg.json"), \
             patch("builtins.open", m):
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

    def test_initialize_answered_locally_in_cli_only(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 3, "method": "initialize",
                                       "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(resp["result"]["protocolVersion"], "2025-03-26")
        self.assertEqual(resp["result"]["serverInfo"]["name"], "mempalace")
        self.assertIn("tools", resp["result"]["capabilities"])
        self.assertEqual(captured, [], "initialize must not be forwarded in cli-only")

    def test_initialize_defaults_protocol_version_when_absent(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 3, "method": "initialize"})
        self.assertEqual(resp["result"]["protocolVersion"], "2024-11-05")
        self.assertEqual(captured, [])

    def test_handshake_methods_answered_locally_in_cli_only(self):
        handle = _build_handle("cli-only")
        expected = {"ping": {}, "resources/list": {"resources": []},
                    "prompts/list": {"prompts": []}}
        for method, result in expected.items():
            resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 9, "method": method})
            self.assertEqual(resp["result"], result, method)
            self.assertEqual(captured, [], f"{method} must not be forwarded in cli-only")

    def test_notifications_swallowed_in_cli_only(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0",
                                       "method": "notifications/initialized"})
        self.assertIsNone(resp)
        self.assertEqual(captured, [])

    def test_unknown_method_rejected_not_forwarded_in_cli_only(self):
        handle = _build_handle("cli-only")
        resp, captured = _run(handle, {"jsonrpc": "2.0", "id": 11,
                                       "method": "logging/setLevel"})
        self.assertEqual(resp["error"]["code"], -32601)
        self.assertEqual(captured, [], "cli-only must never contact the daemon")


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


class TestStartupDaemonGate(unittest.TestCase):
    """main() must only hard-exit on an unreachable daemon in "all" mode.

    cli-only serves the whole MCP surface locally, so an asleep palace host
    (Slumber Ward S3) must not turn the plugin into a red "Failed to connect"
    in every Claude Code session."""

    def _run_main(self, mode, daemon_up):
        calls = {}
        argv = ["mempalace-mcp.py", "--daemon", "http://daemon"]
        with patch.object(proxy.sys, "argv", argv), \
             patch.object(proxy, "find_daemon", lambda url: daemon_up), \
             patch.object(proxy, "resolve_mcp_mode", lambda: mode), \
             patch.object(proxy, "run_daemon_mode",
                          lambda url, m: calls.setdefault("ran", (url, m))), \
             patch("builtins.print"):
            proxy.main()
        return calls

    def test_cli_only_serves_locally_when_daemon_down(self):
        calls = self._run_main("cli-only", daemon_up=False)
        self.assertEqual(calls["ran"], ("http://daemon", "cli-only"))

    def test_all_mode_still_exits_when_daemon_down(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run_main("all", daemon_up=False)
        self.assertEqual(ctx.exception.code, 1)

    def test_all_mode_runs_when_daemon_up(self):
        calls = self._run_main("all", daemon_up=True)
        self.assertEqual(calls["ran"], ("http://daemon", "all"))


class TestNonDictRequestGuard(unittest.TestCase):
    """A non-dict JSON-RPC line (batch list, scalar, null) must not crash —
    request.get(...) would raise AttributeError. Addresses Gemini review on #59."""

    def test_handle_returns_none_for_non_dict(self):
        for mode in ("all", "cli-only"):
            handle = _build_handle(mode)
            for bad in ([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], "scalar", None, 42):
                resp, captured = _run(handle, bad)
                self.assertIsNone(resp, f"mode={mode} input={bad!r}")
                self.assertEqual(captured, [], f"non-dict must not forward (mode={mode})")

    def test_stdio_loop_skips_non_dict_lines(self):
        import io

        # A batch list and a bare null on their own lines, then one valid
        # request. The loop must skip the first two without raising and
        # still dispatch the third.
        lines = "[{\"id\": 1}]\nnull\n{\"jsonrpc\": \"2.0\", \"id\": 7, \"method\": \"ping\"}\n"
        seen = []

        def _handle(req):
            seen.append(req)
            return {"jsonrpc": "2.0", "id": req["id"], "result": {}}

        with patch.object(proxy.sys, "stdin", io.StringIO(lines)), \
             patch("builtins.print"):
            proxy._stdio_loop(_handle)

        # Only the valid dict request reached the handler.
        self.assertEqual(seen, [{"jsonrpc": "2.0", "id": 7, "method": "ping"}])


if __name__ == "__main__":
    unittest.main()
