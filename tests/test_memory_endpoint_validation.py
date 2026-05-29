"""HTTP-level regression tests for POST /memory (palace-daemon #187).

The #187 bug — pydantic v2 skipping field validators on default values —
passed the entire test suite and broke `/memory` in production, caught
only by a manual live-curl probe. The #187 commit noted the root cause of
the *escape*: "no test exercises POST /memory with a missing-room body."

``tests/test_write_surface_models.py`` now covers the MODEL in isolation,
but the model passing doesn't prove the *endpoint* wires it correctly —
the value the handler dispatches to mempalace is what actually matters.
These tests exercise the full request path through ``main.app`` with a
TestClient and assert on the wing/room that reaches the ``mempalace_add_drawer``
dispatch. They fail if MemoryBody's ``validate_default`` is dropped (a
missing room would dispatch ``""`` instead of ``"discoveries"``), which is
exactly the production break #187 fixed.

Run with::

    python -m unittest tests.test_memory_endpoint_validation -v
"""
import json
import os
import sys
import unittest
from unittest.mock import patch, AsyncMock

from fastapi.testclient import TestClient

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


def _fake_mcp_envelope(payload: dict) -> dict:
    """Shape _unwrap expects: result.content[0].text is a JSON string."""
    return {"result": {"content": [{"text": json.dumps(payload)}]}}


async def _stub_novelty(content, wing, room, call):
    """Stand-in for novelty.compute_novelty_for_write — no _call traffic."""
    return {"enabled": False}


class TestMemoryEndpointCanonicalization(unittest.TestCase):
    """Assert the canonicalized wing/room reach the dispatch — the #187
    contract at the HTTP layer."""

    def setUp(self):
        # Empty PALACE_API_KEY → _check_auth is a no-op (same as
        # test_silent_save_validation; auth is covered by test_hook_auth).
        self._env = patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False)
        self._env.start()
        self.client = TestClient(main.app)
        # Capture every _call dispatch so we can inspect the write args.
        self.calls = []

        async def _capture(request_dict, *a, **k):
            self.calls.append(request_dict)
            return _fake_mcp_envelope({"success": True, "drawer_id": "drawer_test"})

        self._call_patch = patch.object(main, "_call", new=AsyncMock(side_effect=_capture))
        self._call_patch.start()
        self._nov_patch = patch("novelty.compute_novelty_for_write", new=_stub_novelty)
        self._nov_patch.start()

    def tearDown(self):
        self._nov_patch.stop()
        self._call_patch.stop()
        self._env.stop()

    def _write_args(self):
        """The arguments dict from the mempalace_add_drawer dispatch."""
        for c in self.calls:
            params = c.get("params", {})
            if params.get("name") == "mempalace_add_drawer":
                return params["arguments"]
        self.fail("no mempalace_add_drawer dispatch was made")

    def test_missing_room_dispatches_discoveries(self):
        # THE #187 regression at the HTTP layer: a POST omitting `room`
        # must dispatch room="discoveries", not "". Without
        # validate_default=True this dispatches "" and mempalace rejects it.
        resp = self.client.post("/memory", json={"content": "x", "wing": "palace_daemon"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._write_args()["room"], "discoveries")

    def test_missing_wing_dispatches_unknown(self):
        resp = self.client.post("/memory", json={"content": "x"})
        self.assertEqual(resp.status_code, 200)
        args = self._write_args()
        self.assertEqual(args["wing"], "unknown")
        self.assertEqual(args["room"], "discoveries")

    def test_wing_normalized_on_dispatch(self):
        resp = self.client.post(
            "/memory",
            json={"content": "x", "wing": "Palace_Daemon", "room": "architecture"},
        )
        self.assertEqual(resp.status_code, 200)
        args = self._write_args()
        self.assertEqual(args["wing"], "palace_daemon")
        self.assertEqual(args["room"], "architecture")

    def test_bad_room_rejected_400_without_dispatch(self):
        resp = self.client.post(
            "/memory",
            json={"content": "x", "wing": "palace_daemon", "room": "bogus_room"},
        )
        self.assertEqual(resp.status_code, 400)
        # Validation must reject BEFORE any write reaches mempalace.
        self.assertEqual(self.calls, [], "bad room must not dispatch a write")

    def test_empty_content_still_dispatches(self):
        # Pre-#179 used body.get("content", "") with no rejection; the
        # endpoint preserves that — empty content is a valid (if odd) write.
        resp = self.client.post("/memory", json={"wing": "palace_daemon"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._write_args()["content"], "")


if __name__ == "__main__":
    unittest.main()
