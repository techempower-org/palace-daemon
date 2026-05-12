"""Regression tests for clients/hook.py auth header + error classification.

Run with::

    cd /path/to/palace-daemon
    python -m unittest tests.test_hook_auth -v

These tests exist because the 2026-05-11 outage was a silent auth failure
that ran for hours — hook.py never sent X-API-Key and the broad
`except Exception` swallowed 401 responses as "daemon unreachable",
sending diagnosis off in the wrong direction.

The fix lives at clients/hook.py:_request_headers + the split error
handlers in _post_mcp and _post_mine. These tests lock in the post-fix
behavior so a future refactor can't drop the header silently again.
"""
import io
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

# Ensure clients/ is on sys.path so `import hook` resolves.
_HERE = os.path.dirname(os.path.abspath(__file__))
_CLIENTS = os.path.join(os.path.dirname(_HERE), "clients")
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import hook  # noqa: E402


class TestRequestHeaders(unittest.TestCase):
    """Lock in the auth-header decision logic."""

    def test_no_api_key_env_returns_content_type_only(self):
        with patch.dict(os.environ, {}, clear=True):
            headers = hook._request_headers()
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertNotIn("X-API-Key", headers)

    def test_with_api_key_env_includes_header(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "abc123"}, clear=True):
            headers = hook._request_headers()
        self.assertEqual(headers["X-API-Key"], "abc123")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_whitespace_only_key_is_treated_as_unset(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "   "}, clear=True):
            headers = hook._request_headers()
        self.assertNotIn("X-API-Key", headers)

    def test_api_key_is_stripped(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "  abc123  "}, clear=True):
            headers = hook._request_headers()
        self.assertEqual(headers["X-API-Key"], "abc123")


class TestPostMcpAuth(unittest.TestCase):
    """Verify _post_mcp sends X-API-Key + classifies failure modes correctly."""

    def _make_response(self, status=200):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_sends_x_api_key_header_when_env_set(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            # Capture the Request object so we can assert on its headers.
            captured["req"] = req
            return self._make_response(200)

        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok = hook._post_mcp("http://daemon:8085", "some_tool", {})

        self.assertTrue(ok)
        # urllib.request.Request stores headers with title-case keys.
        self.assertEqual(captured["req"].get_header("X-api-key"), "the-key")

    def test_http_401_logged_as_rejected_not_unreachable(self):
        """Regression for the 2026-05-11 silent-auth-failure bug."""
        http_err = urllib.error.HTTPError(
            url="http://daemon/mcp", code=401, msg="Unauthorized",
            hdrs=None, fp=None,
        )

        log_messages = []
        with patch.dict(os.environ, {"PALACE_API_KEY": "wrong-key"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=http_err), \
             patch.object(hook, "_log", side_effect=log_messages.append):
            ok = hook._post_mcp("http://daemon:8085", "some_tool", {})

        self.assertFalse(ok)
        # At least one log line should mention the HTTP code, NOT
        # generically say "unreachable" or "network/transport".
        joined = " ".join(log_messages).lower()
        self.assertIn("401", joined)
        self.assertNotIn("network/transport", joined)

    def test_url_error_logged_as_transport(self):
        url_err = urllib.error.URLError("Connection refused")

        log_messages = []
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=url_err), \
             patch.object(hook, "_log", side_effect=log_messages.append):
            ok = hook._post_mcp("http://daemon:8085", "some_tool", {})

        self.assertFalse(ok)
        joined = " ".join(log_messages).lower()
        self.assertIn("network/transport", joined)
        self.assertNotIn("401", joined)


class TestPostMineAuth(unittest.TestCase):
    """Same shape as _post_mcp but with the /mine-specific message that
    explicitly names PALACE_API_KEY (so the next operator knows where to
    look)."""

    def _make_response(self, status=200):
        resp = MagicMock()
        resp.status = status
        resp.__enter__ = MagicMock(return_value=resp)
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def test_sends_x_api_key_header_when_env_set(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return self._make_response(200)

        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=fake_urlopen):
            ok = hook._post_mine("http://daemon:8085", "/some/dir")

        self.assertTrue(ok)
        self.assertEqual(captured["req"].get_header("X-api-key"), "the-key")

    def test_http_401_message_mentions_palace_api_key(self):
        """The /mine handler should name the env var explicitly — that
        was the single most diagnostic signal we needed today."""
        http_err = urllib.error.HTTPError(
            url="http://daemon/mine", code=401, msg="Unauthorized",
            hdrs=None, fp=None,
        )

        log_messages = []
        with patch.dict(os.environ, {"PALACE_API_KEY": "wrong"}, clear=True), \
             patch.object(hook.urllib.request, "urlopen", side_effect=http_err), \
             patch.object(hook, "_log", side_effect=log_messages.append):
            ok = hook._post_mine("http://daemon:8085", "/some/dir")

        self.assertFalse(ok)
        joined = " ".join(log_messages)
        self.assertIn("401", joined)
        self.assertIn("PALACE_API_KEY", joined)


if __name__ == "__main__":
    unittest.main()
