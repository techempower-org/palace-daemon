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

    def setUp(self):
        # Never read the real daemon env file from a test.
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.env_file = os.path.join(self._tmp.name, "env")
        self._p = patch.object(hook, "DAEMON_ENV_FILE", self.env_file)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        self._tmp.cleanup()

    def _write_env(self, text):
        with open(self.env_file, "w", encoding="utf-8") as f:
            f.write(text)

    def test_no_api_key_env_returns_content_type_only(self):
        with patch.dict(os.environ, {}, clear=True):
            headers = hook._request_headers()
        self.assertEqual(headers, {"Content-Type": "application/json"})
        self.assertNotIn("X-API-Key", headers)

    def test_env_file_supplies_key_when_env_unset(self):
        self._write_env("MEMPALACE_BACKEND=postgres\nPALACE_API_KEY=from-file\n")
        with patch.dict(os.environ, {}, clear=True):
            headers = hook._request_headers()
        self.assertEqual(headers["X-API-Key"], "from-file")

    def test_env_file_matched_quotes(self):
        for raw in ("'quoted-key'", '"quoted-key"'):
            self._write_env("# comment\nPALACE_API_KEY=%s\n" % raw)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(hook._api_key(), "quoted-key")

    def test_export_line_is_ignored_like_systemd(self):
        # EnvironmentFile= does not understand `export`; neither may we.
        self._write_env("PALACE_API_KEY=real\nexport PALACE_API_KEY=shell-only\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_similar_names_are_not_the_key(self):
        self._write_env("PALACE_API_KEY=real\nPALACE_API_KEYS=other\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_multiline_quoted_value_refuses(self):
        # systemd reads this as one OTHER value; the inner line is not a key.
        self._write_env("OTHER='first\nPALACE_API_KEY=not-the-key\nlast'\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "")

    def test_line_continuation_refuses(self):
        self._write_env("OTHER=a \\\nPALACE_API_KEY=not-the-key\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "")

    def test_escapes_and_spaces_outside_subset_refuse(self):
        for raw in ('"a\\"b"', "a b", "'mixed\"", "\"'inner'\"", ""):
            self._write_env("PALACE_API_KEY=%s\n" % raw)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(hook._api_key(), "", raw)

    def test_environment_wins_and_file_is_not_opened(self):
        self._write_env("PALACE_API_KEY=from-file\n")
        with patch.dict(os.environ, {"PALACE_API_KEY": "from-env"}, clear=True), \
                patch("builtins.open", side_effect=AssertionError("opened")):
            self.assertEqual(hook._api_key(), "from-env")

    def test_last_assignment_wins_like_systemd(self):
        self._write_env("PALACE_API_KEY=old\nOTHER=1\nPALACE_API_KEY=new\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "new")

    def test_comment_lines_are_skipped(self):
        self._write_env("PALACE_API_KEY=real\n# PALACE_API_KEY=commented\n; PALACE_API_KEY=x\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_non_newline_separators_cannot_forge_a_key(self):
        for sep in ("\v", "\f", "\x85", "\u2028", "\u2029", "\x1c"):
            for first in ("PALACE_API_KEY=real", "OTHER=x"):
                self._write_env(first + sep + "PALACE_API_KEY=forged\n")
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(hook._api_key(), "", repr(sep))

    def test_crlf_line_endings_are_accepted(self):
        self._write_env("OTHER=1\r\nPALACE_API_KEY=real\r\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_escaped_quote_cannot_hide_an_open_value(self):
        for tail in ("", "\n"):
            self._write_env('OTHER="a\\"\nPALACE_API_KEY=forged' + tail)
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual(hook._api_key(), "")

    def test_blank_lines_and_no_final_newline_are_fine(self):
        self._write_env("\nOTHER=1\n\n   \nPALACE_API_KEY=real")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_quoted_fragments_elsewhere_refuse(self):
        for other in ("OTHER='\"'\"", "OTHER='a'", 'OTHER="a"', "OTHER=a\\b"):
            for tail in ("", "\n"):
                self._write_env("PALACE_API_KEY=real\n" + other + "\nPALACE_API_KEY=forged" + tail)
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(hook._api_key(), "", other)

    def test_quotes_in_comments_are_fine(self):
        self._write_env("# don't \"quote\" me\nPALACE_API_KEY=real\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "real")

    def test_comment_continuation_cannot_forge_a_key(self):
        for prefix in ("#", ";"):
            for tail in ("", "\n"):
                self._write_env(
                    "PALACE_API_KEY=real\n" + prefix + " continued \\\nPALACE_API_KEY=forged" + tail
                )
                with patch.dict(os.environ, {}, clear=True):
                    self.assertEqual(hook._api_key(), "", prefix)

    def test_unbalanced_quote_refuses(self):
        self._write_env("PALACE_API_KEY='unmatched\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "")

    def test_undecodable_file_yields_no_key(self):
        with open(self.env_file, "wb") as f:
            f.write(b"PALACE_API_KEY=abc\xff\xfe\n")
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "")

    def test_oversized_file_yields_no_key(self):
        self._write_env("PALACE_API_KEY=k\n" + "x" * (hook._DAEMON_ENV_MAX_BYTES + 1))
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(hook._api_key(), "")

    def test_unreadable_file_yields_no_key(self):
        self._write_env("PALACE_API_KEY=k\n")
        with patch.dict(os.environ, {}, clear=True), \
                patch("builtins.open", side_effect=PermissionError("denied")):
            self.assertEqual(hook._api_key(), "")

    def test_whitespace_env_falls_back_to_file(self):
        self._write_env("PALACE_API_KEY=from-file\n")
        with patch.dict(os.environ, {"PALACE_API_KEY": "  "}, clear=True):
            self.assertEqual(hook._api_key(), "from-file")

    def test_env_file_without_key_sends_no_header(self):
        self._write_env("PALACE_API_KEYS=not-this\nOTHER=1\n")
        with patch.dict(os.environ, {}, clear=True):
            headers = hook._request_headers()
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
            ok, _ = hook._post_mcp("http://daemon:8085", "some_tool", {})

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
            ok, _ = hook._post_mcp("http://daemon:8085", "some_tool", {})

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
            ok, _ = hook._post_mcp("http://daemon:8085", "some_tool", {})

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
            ok, _ = hook._post_mine("http://daemon:8085", "/some/dir")

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
            ok, _ = hook._post_mine("http://daemon:8085", "/some/dir")

        self.assertFalse(ok)
        joined = " ".join(log_messages)
        self.assertIn("401", joined)
        self.assertIn("PALACE_API_KEY", joined)


if __name__ == "__main__":
    unittest.main()
