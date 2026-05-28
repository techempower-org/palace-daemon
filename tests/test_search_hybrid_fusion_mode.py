"""Tests for `/search/hybrid?fusion_mode=...` parameter (#105).

mempalace#162 (merged as #295) added `fusion_mode="rrf"` as an opt-in
alongside the default convex blend. palace-daemon's `/search/hybrid`
endpoint now accepts and forwards the parameter so callers can A/B
convex vs RRF against the production palace. End-to-end behaviour is
gated on mempalace#298 (adding `fusion_mode` to the MCP input schema);
these tests verify the daemon-side surface is correct and forward-
compatible.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_search_hybrid_fusion_mode.py -q
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Avoid the test_admin_refresh_rooms-style import error by checking fastapi
try:
    from fastapi.testclient import TestClient  # noqa: F401
    HAVE_FASTAPI = True
except ImportError:
    HAVE_FASTAPI = False

import main  # noqa: E402
import rooms  # noqa: E402  — #101 twelfth slice: canonical-rooms cache lives here


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed in test env")
class TestSearchHybridFusionMode(unittest.IsolatedAsyncioTestCase):
    """The endpoint accepts fusion_mode and forwards it (when valid)."""

    def setUp(self):
        # Use a fake config object — `backend` and `palace_path` are properties
        # without setters on the real config, so patch.object on the attribute
        # won't work; replace the whole _config instead.
        fake_config = type("FakeConfig", (), {
            "backend": "postgres",
            "palace_path": "/srv/test-palace",
        })()
        self._config_patch = patch.object(main._mp, "_config", fake_config)
        self._config_patch.start()
        self._rooms_patch = patch.object(main, "_canonical_rooms", return_value={"general", "planning"})
        self._rooms_patch.start()
        self._auth_patch = patch.object(main, "_check_auth")
        self._auth_patch.start()
        self._rerank_patch = patch.object(main._rerank, "rerank_response", side_effect=lambda q, r: r)
        self._rerank_patch.start()

    def tearDown(self):
        self._config_patch.stop()
        self._rooms_patch.stop()
        self._auth_patch.stop()
        self._rerank_patch.stop()

    async def _call(self, body: dict):
        """Invoke search_hybrid with a fake Request, return either response or HTTPException."""
        from fastapi import HTTPException
        req = MagicMock()
        req.json = AsyncMock(return_value=body)
        captured_args = {}

        async def fake_call(envelope):
            captured_args["args"] = envelope["params"]["arguments"]
            return {"jsonrpc": "2.0", "id": 1, "result": {
                "content": [{"type": "text", "text": '{"results":[]}'}]
            }}

        with patch.object(main, "_call", side_effect=fake_call):
            try:
                result = await main.search_hybrid(req, x_api_key="test-key")
                return ("ok", result, captured_args.get("args"))
            except HTTPException as e:
                return ("http_error", e.status_code, e.detail)

    async def test_no_fusion_mode_omits_from_args(self):
        """If caller doesn't pass fusion_mode, the args dict should not carry it."""
        kind, _, args = await self._call({"query": "test"})
        self.assertEqual(kind, "ok")
        self.assertNotIn("fusion_mode", args)

    async def test_fusion_mode_convex_forwarded(self):
        kind, _, args = await self._call({"query": "test", "fusion_mode": "convex"})
        self.assertEqual(kind, "ok")
        self.assertEqual(args.get("fusion_mode"), "convex")

    async def test_fusion_mode_rrf_forwarded(self):
        kind, _, args = await self._call({"query": "test", "fusion_mode": "rrf"})
        self.assertEqual(kind, "ok")
        self.assertEqual(args.get("fusion_mode"), "rrf")

    async def test_fusion_mode_invalid_string_rejected(self):
        kind, status, detail = await self._call({"query": "test", "fusion_mode": "magic"})
        self.assertEqual(kind, "http_error")
        self.assertEqual(status, 400)
        self.assertIn("fusion_mode", detail)

    async def test_fusion_mode_non_string_rejected(self):
        kind, status, detail = await self._call({"query": "test", "fusion_mode": 42})
        self.assertEqual(kind, "http_error")
        self.assertEqual(status, 400)

    async def test_fusion_mode_null_treated_as_omitted(self):
        """Explicit JSON null behaves the same as omitting the key."""
        kind, _, args = await self._call({"query": "test", "fusion_mode": None})
        self.assertEqual(kind, "ok")
        self.assertNotIn("fusion_mode", args)


if __name__ == "__main__":
    unittest.main()
