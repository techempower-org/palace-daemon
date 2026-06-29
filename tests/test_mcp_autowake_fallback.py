"""Tests for the MCP proxy's auto-wake fallback in ``_forward_with_autowake``.

The palace host is Slumber-Ward sleepable, so a ``URLError`` on the first
forward usually means "asleep", not "down". The proxy then tries one
Wake-on-LAN cycle and retries. This locks in the fallback contract:

  * unreachable + NO auto_wake configured → an "unreachable" result that does
    NOT claim a WoL was sent (regression: it used to lie "WoL sent");
  * unreachable + auto_wake configured, host still down after the retry →
    the friendly "waking up (WoL sent)" result;
  * the retry forward raising an ``HTTPError`` (host woke but rejects) →
    the real 4xx/5xx surfaced, not masked as "still waking";
  * a successful retry → the daemon's real result passed through.

Everything is mocked — no real daemon, no real Wake-on-LAN, no sleep.

The proxy module has a hyphen in its filename so it can't be imported by
name; we load it via importlib (mirrors test_mcp_mode_gate.py).

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_mcp_autowake_fallback.py -q
"""
import importlib.util
import os
import unittest
import urllib.error
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROXY_PATH = os.path.join(os.path.dirname(_HERE), "clients", "mempalace-mcp.py")

_spec = importlib.util.spec_from_file_location("mempalace_mcp_proxy", _PROXY_PATH)
proxy = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(proxy)


def _http_error(code=401, reason="Unauthorized"):
    return urllib.error.HTTPError("http://daemon", code, reason, {}, None)


def _url_error(reason="Connection refused"):
    return urllib.error.URLError(reason)


REQUEST = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {}}


class TestForwardWithAutowake(unittest.TestCase):
    def _text(self, resp):
        return resp["result"]["content"][0]["text"]

    def test_unreachable_no_wake_does_not_claim_wol_sent(self):
        """No auto_wake configured → don't lie about sending a WoL."""
        with patch.object(proxy, "forward", side_effect=_url_error()), \
             patch.object(proxy, "_load_auto_wake_command", return_value=""), \
             patch.object(proxy.subprocess, "run") as run:
            resp = proxy._forward_with_autowake("http://daemon", REQUEST)
        text = self._text(resp)
        self.assertIn("unreachable", text.lower())
        self.assertNotIn("WoL sent", text)
        self.assertIn("mempalace search", text)
        self.assertFalse(resp["result"]["isError"])
        run.assert_not_called()  # nothing to run
        self.assertEqual(resp["id"], 7)

    def test_unreachable_with_wake_reports_waking(self):
        """auto_wake configured, host still down after retry → 'waking up'."""
        with patch.object(proxy, "forward", side_effect=_url_error()), \
             patch.object(proxy, "_load_auto_wake_command",
                          return_value="realm wol wake familiar"), \
             patch.object(proxy.subprocess, "run") as run, \
             patch.object(proxy.time, "sleep"):
            resp = proxy._forward_with_autowake("http://daemon", REQUEST)
        text = self._text(resp)
        self.assertIn("WoL sent", text)
        self.assertIn("waking up", text.lower())
        run.assert_called_once()  # the wake command fired
        self.assertEqual(run.call_args.args[0], ["realm", "wol", "wake", "familiar"])

    def test_retry_http_error_is_surfaced_not_masked(self):
        """Host woke but rejects on retry (4xx/5xx) → surface the real error."""
        calls = {"n": 0}

        def forward(_url, _req):
            calls["n"] += 1
            raise _url_error() if calls["n"] == 1 else _http_error(500, "Server Error")

        with patch.object(proxy, "forward", side_effect=forward), \
             patch.object(proxy, "_load_auto_wake_command",
                          return_value="realm wol wake familiar"), \
             patch.object(proxy.subprocess, "run"), \
             patch.object(proxy.time, "sleep"):
            resp = proxy._forward_with_autowake("http://daemon", REQUEST)
        self.assertIn("error", resp)
        self.assertIn("HTTP 500 Server Error", resp["error"]["message"])
        self.assertNotIn("result", resp)

    def test_initial_http_error_is_surfaced(self):
        """A 4xx/5xx on the very first forward means the daemon is awake and
        rejecting — never trigger a wake, return the real error."""
        with patch.object(proxy, "forward", side_effect=_http_error(401, "Unauthorized")), \
             patch.object(proxy, "_load_auto_wake_command") as load, \
             patch.object(proxy.subprocess, "run") as run:
            resp = proxy._forward_with_autowake("http://daemon", REQUEST)
        self.assertIn("HTTP 401 Unauthorized", resp["error"]["message"])
        load.assert_not_called()  # never reached the wake branch
        run.assert_not_called()

    def test_successful_retry_passes_through(self):
        """Host wakes and the retry succeeds → return the daemon's result."""
        calls = {"n": 0}

        def forward(_url, _req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _url_error()
            return {"jsonrpc": "2.0", "id": 7, "result": {"forwarded": True}}

        with patch.object(proxy, "forward", side_effect=forward), \
             patch.object(proxy, "_load_auto_wake_command",
                          return_value="realm wol wake familiar"), \
             patch.object(proxy.subprocess, "run"), \
             patch.object(proxy.time, "sleep"):
            resp = proxy._forward_with_autowake("http://daemon", REQUEST)
        self.assertEqual(resp["result"], {"forwarded": True})


if __name__ == "__main__":
    unittest.main()
