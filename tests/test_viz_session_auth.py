"""Tests for the /viz session-cookie auth hardening.

The API key must never be accepted in the URL. /viz is bookmarkable via a
short-lived, HMAC-signed, HttpOnly ``palace_viz_session`` cookie minted by
``POST /viz/session``. The cookie is honoured only by the read-only GET
surface the dashboard reads (/viz, /graph, /backfill-age/status); write
endpoints stay header-only so the cookie can't be replayed cross-site.

Covers token mint/verify, ``_check_viz_auth`` precedence, the no-op path
when PALACE_API_KEY is unset, and the /viz/session set-cookie response.

Run with::

    python -m unittest tests.test_viz_session_auth -v
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
from fastapi import HTTPException  # noqa: E402

_KEY = "s3cret-key"


class TestVizToken(unittest.TestCase):
    def test_mint_then_verify_roundtrips(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            tok = main._mint_viz_token()
            self.assertTrue(main._valid_viz_token(tok))

    def test_expired_token_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            # Mint with a TTL in the past.
            with patch.object(main, "PALACE_VIZ_SESSION_TTL_SECONDS", -10):
                tok = main._mint_viz_token()
            self.assertFalse(main._valid_viz_token(tok))

    def test_tampered_signature_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            tok = main._mint_viz_token()
            exp, _, _sig = tok.partition(".")
            forged = f"{exp}.{'0' * 64}"
            self.assertFalse(main._valid_viz_token(forged))

    def test_token_signed_with_other_key_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            tok = main._mint_viz_token()
        # Same token, but the server's key has since changed.
        with patch.dict(os.environ, {"PALACE_API_KEY": "different-key"}):
            self.assertFalse(main._valid_viz_token(tok))

    def test_malformed_tokens_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            for bad in (None, "", "nodot", "abc.def", ".", "123.", "x.y.z"):
                self.assertFalse(main._valid_viz_token(bad), bad)

    def test_no_api_key_means_no_valid_token(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}):
            self.assertFalse(main._valid_viz_token("anything.sig"))


class TestCheckVizAuth(unittest.TestCase):
    def test_noop_when_key_unset(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}):
            # Neither header nor cookie, but no key configured → must not raise.
            main._check_viz_auth(None, None)

    def test_valid_header_accepted(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            main._check_viz_auth(_KEY, None)

    def test_valid_cookie_accepted(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            tok = main._mint_viz_token()
            main._check_viz_auth(None, tok)

    def test_wrong_header_but_valid_cookie_accepted(self):
        # Header check must fall through to the cookie, not hard-fail.
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            tok = main._mint_viz_token()
            main._check_viz_auth("wrong", tok)

    def test_wrong_header_no_cookie_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            with self.assertRaises(HTTPException) as cm:
                main._check_viz_auth("wrong", None)
            self.assertEqual(cm.exception.status_code, 401)

    def test_no_creds_rejected_when_key_set(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            with self.assertRaises(HTTPException) as cm:
                main._check_viz_auth(None, None)
            self.assertEqual(cm.exception.status_code, 401)

    def test_invalid_cookie_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            with self.assertRaises(HTTPException):
                main._check_viz_auth(None, "bogus.token")


class TestVizSessionEndpoint(unittest.IsolatedAsyncioTestCase):
    async def test_sets_cookie_on_valid_key(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            resp = await main.viz_session(x_api_key=_KEY)
        self.assertEqual(resp.status_code, 200)
        set_cookie = resp.headers.get("set-cookie", "")
        self.assertIn(main._VIZ_COOKIE_NAME, set_cookie)
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=lax", set_cookie.lower())

    async def test_rejects_wrong_key(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": _KEY}):
            with self.assertRaises(HTTPException) as cm:
                await main.viz_session(x_api_key="wrong")
            self.assertEqual(cm.exception.status_code, 401)

    async def test_no_cookie_when_auth_disabled(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": ""}):
            resp = await main.viz_session(x_api_key=None)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("set-cookie", {k.lower() for k in resp.headers})


if __name__ == "__main__":
    unittest.main()
