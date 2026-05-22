"""Regression test for _check_auth using constant-time comparison.

Locks in the fix for issue #31: ``_check_auth`` must compare the
X-API-Key header against ``PALACE_API_KEY`` with ``hmac.compare_digest``,
not with ``str.__eq__``. The intent is timing-attack hardening —
verifying the *behavior* (accepts the right key, rejects everything
else, no early exit on None) is what we can assert; the constant-time
property itself is enforced by importing ``hmac`` and using
``hmac.compare_digest``, which this test pins through the public-facing
accept/reject matrix and a spy on the comparison call.

Run with::

    cd /home/jp/Projects/palace-daemon
    source venv/bin/activate
    python -m unittest tests.test_auth_constant_time -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from fastapi import HTTPException  # noqa: E402

import main  # noqa: E402
from main import _check_auth  # noqa: E402


class TestCheckAuthConstantTime(unittest.TestCase):
    def test_no_env_key_accepts_anything(self):
        """When PALACE_API_KEY is unset/empty, auth is open (legacy behavior)."""
        with patch.dict(os.environ, {}, clear=True):
            _check_auth(None)
            _check_auth("")
            _check_auth("anything")

    def test_correct_key_accepted(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True):
            _check_auth("the-key")

    def test_wrong_key_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                _check_auth("wrong")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_missing_header_rejected_when_key_required(self):
        """A missing X-API-Key header (None) must NOT short-circuit
        through a fast path — that would reintroduce a timing distinction
        between 'header absent' and 'header wrong'."""
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                _check_auth(None)
            self.assertEqual(ctx.exception.status_code, 401)

    def test_empty_header_rejected_when_key_required(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True):
            with self.assertRaises(HTTPException) as ctx:
                _check_auth("")
            self.assertEqual(ctx.exception.status_code, 401)

    def test_routes_through_hmac_compare_digest(self):
        """The implementation must route through ``hmac.compare_digest``.
        That is what gives us constant-time. If a future refactor
        regresses back to ``!=``, this test fails."""
        original = main.hmac.compare_digest
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=True), \
             patch.object(main.hmac, "compare_digest", wraps=original) as spy:
            _check_auth("the-key")
            spy.assert_called_once_with("the-key", "the-key")


if __name__ == "__main__":
    unittest.main()
