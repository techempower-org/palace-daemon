"""Regression tests for POST /admin/refresh-rooms.

Locks in the cache-invalidation contract:

    * The cached canonical-rooms set is dropped *before* the eager
      rebuild — a stale value never wins.
    * The endpoint repopulates the cache eagerly and returns the new
      list plus a count.
    * Auth follows the standard ``X-API-Key`` model (no separate admin
      token); a wrong key is 401.

Run with::

    cd /home/jp/Projects/palace-daemon
    python -m unittest tests.test_admin_refresh_rooms -v
"""
import os
import sys
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


class TestRefreshRoomsBehavior(unittest.TestCase):
    """Endpoint clears the cache and returns the rebuilt list."""

    def setUp(self):
        # Empty PALACE_API_KEY → _check_auth is a no-op. Auth itself
        # is exercised in TestRefreshRoomsAuth below.
        self._env_patch = patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False)
        self._env_patch.start()
        self.client = TestClient(main.app)
        # Always start with a clean module-level cache so test order
        # cannot cross-contaminate.
        main._canonical_rooms_cache = None

    def tearDown(self):
        main._canonical_rooms_cache = None
        self._env_patch.stop()

    def test_endpoint_clears_cache_before_rebuilding(self):
        """The cache must be set to None *before* _canonical_rooms() runs,
        otherwise a stale entry would short-circuit the rebuild path."""
        # Seed the cache with a sentinel set that nothing else would produce.
        main._canonical_rooms_cache = {"stale-room-from-old-process"}

        observed_cache_at_rebuild = []

        def fake_canonical_rooms():
            # When the endpoint calls us, the cache must already have been cleared.
            observed_cache_at_rebuild.append(main._canonical_rooms_cache)
            main._canonical_rooms_cache = {"alpha", "beta", "gamma"}
            return main._canonical_rooms_cache

        with patch.object(main, "_canonical_rooms", side_effect=fake_canonical_rooms):
            resp = self.client.post("/admin/refresh-rooms")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(observed_cache_at_rebuild, [None])

    def test_response_shape(self):
        """Response carries refreshed=True, sorted rooms, and count."""
        with patch.object(main, "_canonical_rooms", return_value={"gamma", "alpha", "beta"}):
            resp = self.client.post("/admin/refresh-rooms")

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["refreshed"], True)
        self.assertEqual(body["rooms"], ["alpha", "beta", "gamma"])  # sorted
        self.assertEqual(body["count"], 3)

    def test_count_matches_room_list_length(self):
        """count is derived from the returned list — never drifts."""
        with patch.object(main, "_canonical_rooms",
                          return_value={f"room-{i}" for i in range(7)}):
            resp = self.client.post("/admin/refresh-rooms")

        body = resp.json()
        self.assertEqual(body["count"], len(body["rooms"]))

    def test_uses_get_method_returns_405(self):
        """The endpoint is POST-only — a GET must not silently succeed.

        This pins the side-effecting verb so callers don't end up
        clearing the cache from a curl typo or a browser preview."""
        resp = self.client.get("/admin/refresh-rooms")
        self.assertEqual(resp.status_code, 405)


class TestRefreshRoomsAuth(unittest.TestCase):
    """X-API-Key enforcement — same model as every other endpoint."""

    def setUp(self):
        self.client = TestClient(main.app)
        main._canonical_rooms_cache = None

    def tearDown(self):
        main._canonical_rooms_cache = None

    def test_wrong_key_rejected(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=False):
            with patch.object(main, "_canonical_rooms", return_value={"alpha"}):
                resp = self.client.post(
                    "/admin/refresh-rooms",
                    headers={"X-API-Key": "wrong"},
                )
        self.assertEqual(resp.status_code, 401)

    def test_missing_key_rejected_when_required(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=False):
            with patch.object(main, "_canonical_rooms", return_value={"alpha"}):
                resp = self.client.post("/admin/refresh-rooms")
        self.assertEqual(resp.status_code, 401)

    def test_correct_key_accepted(self):
        with patch.dict(os.environ, {"PALACE_API_KEY": "the-key"}, clear=False):
            with patch.object(main, "_canonical_rooms", return_value={"alpha"}):
                resp = self.client.post(
                    "/admin/refresh-rooms",
                    headers={"X-API-Key": "the-key"},
                )
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
