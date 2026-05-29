"""HTTP-level test: per-request rerank toggle through the extracted route (#189 + #101 #3).

test_rerank_per_request covers rerank_hits/rerank_response in isolation;
this drives the full request path through ``main.app`` → the extracted
``search_routes.search`` handler → ``main._call`` / ``main._rerank`` and
asserts the ``?rerank=`` query param reaches rerank_response's ``enabled``
override. It's the end-to-end proof that:

  * the #101 #3 extraction wired the handler correctly (the handler resolves
    main._call / main._unwrap / main._search_args / main._rerank via the
    lazy-``import main`` indirection), and
  * the #189 ``rerank`` query param threads through to the rerank block.

``main._call`` is patched to return a fake hit so no palace is needed; the
real ``rerank_response`` runs, and its ``enabled`` / ``enabled_source``
fields record the per-request decision regardless of whether a FlashRank
model is installed (the gate decision happens before model load).

Run with::

    python -m unittest tests.test_search_rerank_endpoint -v
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
    return {"result": {"content": [{"text": json.dumps(payload)}]}}


class TestSearchRerankEndpoint(unittest.TestCase):

    def setUp(self):
        self._env = patch.dict(
            os.environ,
            {"PALACE_API_KEY": "", "PALACE_RERANK_ENABLED": "true"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(main.app)

        async def _fake_call(request_dict, *a, **k):
            # A single hit with rerankable text so rerank_hits has something
            # to act on; the gate decision is what we assert.
            return _fake_mcp_envelope({"results": [{"drawer_id": "d1", "text": "pgvector lock note"}]})

        self._call_patch = patch.object(main, "_call", new=AsyncMock(side_effect=_fake_call))
        self._call_patch.start()

    def tearDown(self):
        self._call_patch.stop()
        self._env.stop()

    def _rerank_block(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json().get("rerank", {})

    def test_rerank_false_forces_off_per_request(self):
        rb = self._rerank_block("/search?q=pgvector%20lock&limit=2&rerank=false")
        self.assertFalse(rb.get("enabled"))
        self.assertEqual(rb.get("enabled_source"), "per-request")

    def test_rerank_true_forces_on_per_request(self):
        rb = self._rerank_block("/search?q=pgvector%20lock&limit=2&rerank=true")
        self.assertTrue(rb.get("enabled"))
        self.assertEqual(rb.get("enabled_source"), "per-request")

    def test_rerank_absent_defers_to_env(self):
        rb = self._rerank_block("/search?q=pgvector%20lock&limit=2")
        self.assertEqual(rb.get("enabled_source"), "env")
        # env says true above
        self.assertTrue(rb.get("enabled"))


if __name__ == "__main__":
    unittest.main()
