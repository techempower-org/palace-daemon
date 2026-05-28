"""Tests for /health HTTP-code semantics around crash_loop (#143).

The crash_loop signal is *informational* — it means the daemon has
restarted several times in the configured window. It does NOT mean the
daemon is broken. A daemon that's serving traffic fine but happens to
have been deployed 3 times in 10 minutes is NOT a service that needs
auto-restart by monitoring tools.

Pre-#143 /health returned 503 for crash_loop=True, which mis-signaled
to systemd/loadbalancer/manual operators that the service was failing.
Post-#143:

- 200 + status="ok" when palace_ok=True (regardless of crash_loop)
- 503 + status="degraded" only when palace_ok=False
- crash_loop / restart_count / window_seconds / uptime_seconds fields
  remain in the body for observability

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_health_crash_loop_not_503.py -v
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestHealthHttpCodes(unittest.IsolatedAsyncioTestCase):
    """The crash_loop signal must not drive the HTTP code."""

    def _unwrap(self, resp):
        """Coerce either a raw dict (200 OK passthrough) or JSONResponse to (status_code, payload)."""
        if isinstance(resp, dict):
            return 200, resp
        # JSONResponse — body is bytes; status_code is the HTTP code.
        return resp.status_code, json.loads(resp.body)

    async def test_crash_loop_with_palace_ok_returns_200(self):
        """crash_loop=True + palace_ok=True → 200, status=ok."""
        # Mock _mp.handle_request to look successful, _get_collection to return a
        # truthy object, and _crash_loop_state to report active crash_loop.
        fake_col = MagicMock()
        with patch.object(main._mp, "handle_request", return_value={"jsonrpc": "2.0", "id": 1, "result": {}}), \
             patch.object(main._mp, "_get_collection", return_value=fake_col), \
             patch.object(main, "_crash_loop_state", return_value={
                 "crash_loop": True, "restart_count": 5,
                 "window_seconds": 600, "uptime_seconds": 30.0,
                 "recovered": False,
             }), \
             patch.object(main, "_db_errors_summary", return_value={"last_300s": {"total": 0}}), \
             patch.object(main, "_postgres_memcg_status", return_value=None):
            resp = await main.health()
        code, body = self._unwrap(resp)
        self.assertEqual(code, 200, "crash_loop alone must not produce 503")
        self.assertEqual(body["status"], "ok")
        # crash_loop signal still surfaced as observability data.
        self.assertTrue(body["crash_loop"])
        self.assertEqual(body["restart_count"], 5)

    async def test_palace_unreachable_returns_503(self):
        """palace_ok=False → 503, status=degraded."""
        # _get_collection raises → palace_ok=False.
        with patch.object(main._mp, "handle_request", return_value={"jsonrpc": "2.0", "id": 1, "result": {}}), \
             patch.object(main._mp, "_get_collection", side_effect=RuntimeError("postgres down")), \
             patch.object(main, "_crash_loop_state", return_value={
                 "crash_loop": False, "restart_count": 0,
                 "window_seconds": 600, "uptime_seconds": 100.0,
                 "recovered": False,
             }), \
             patch.object(main, "_db_errors_summary", return_value={"last_300s": {"total": 0}}), \
             patch.object(main, "_postgres_memcg_status", return_value=None):
            resp = await main.health()
        code, body = self._unwrap(resp)
        self.assertEqual(code, 503)
        self.assertEqual(body["status"], "degraded")

    async def test_palace_unreachable_AND_crash_loop_still_503(self):
        """If both signals are bad, 503 wins (palace_ok dominates)."""
        with patch.object(main._mp, "handle_request", return_value={"jsonrpc": "2.0", "id": 1, "result": {}}), \
             patch.object(main._mp, "_get_collection", side_effect=RuntimeError("down")), \
             patch.object(main, "_crash_loop_state", return_value={
                 "crash_loop": True, "restart_count": 3,
                 "window_seconds": 600, "uptime_seconds": 30.0,
                 "recovered": False,
             }), \
             patch.object(main, "_db_errors_summary", return_value={"last_300s": {"total": 0}}), \
             patch.object(main, "_postgres_memcg_status", return_value=None):
            resp = await main.health()
        code, body = self._unwrap(resp)
        self.assertEqual(code, 503)
        self.assertEqual(body["status"], "degraded")
        # crash_loop fields still surfaced.
        self.assertTrue(body["crash_loop"])

    async def test_clean_state_returns_200_ok(self):
        """palace_ok=True + crash_loop=False → 200, status=ok."""
        fake_col = MagicMock()
        with patch.object(main._mp, "handle_request", return_value={"jsonrpc": "2.0", "id": 1, "result": {}}), \
             patch.object(main._mp, "_get_collection", return_value=fake_col), \
             patch.object(main, "_crash_loop_state", return_value={
                 "crash_loop": False, "restart_count": 0,
                 "window_seconds": 600, "uptime_seconds": 2000.0,
                 "recovered": False,
             }), \
             patch.object(main, "_db_errors_summary", return_value={"last_300s": {"total": 0}}), \
             patch.object(main, "_postgres_memcg_status", return_value=None):
            resp = await main.health()
        code, body = self._unwrap(resp)
        self.assertEqual(code, 200)
        self.assertEqual(body["status"], "ok")


if __name__ == "__main__":
    unittest.main()
