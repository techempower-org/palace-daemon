"""Regression test: kind= checkpoint filter on /search and /context (#194).

Background: commit 4a318d3 retired the kind= filter when the Phase A–E
checkpoint-collection split emptied checkpoints out of ``mempalace_drawers``.
The 2026-05-29 DB rebackfill re-merged checkpoint drawers back into the main
collection, so ``/search?kind=content`` started returning byte-identical
results to ``kind=all`` — including the ``CHECKPOINT:`` stop-hook drawers it
is supposed to exclude (techempower-org/palace-daemon#194). This test pins:

  * kind=content excludes checkpoint hits (by topic OR by CHECKPOINT: body),
  * kind=all is the unfiltered superset,
  * kind=checkpoint returns only the checkpoints,
  * content ⊊ all (the SME behavioural invariant that surfaced the bug),
  * a bad kind= 400s.

The HTTP-level tests drive the full request path through ``main.app`` → the
extracted ``search_routes`` handlers with ``main._call`` patched to a fake
envelope, mirroring tests/test_search_rerank_endpoint.py. Rerank is forced
off so the assertions are about filtering, not reordering.

Run with::

    python -m unittest tests.test_search_kind_filter -v
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
import search_routes  # noqa: E402


def _fake_mcp_envelope(payload: dict) -> dict:
    return {"result": {"content": [{"text": json.dumps(payload)}]}}


# A mixed result set: 2 checkpoint hits (one tagged by topic, one only by
# CHECKPOINT: body to exercise the metadata-loss fallback from #194) and 2
# real content hits.
_CHECKPOINT_BY_TOPIC = {
    "drawer_id": "ckpt1",
    "text": "Session summary: built the kind filter.",
    "topic": "checkpoint",
    "wing": "palace_daemon",
    "room": "sessions",
}
_CHECKPOINT_BY_BODY = {
    # No topic metadata — mimics the rebackfill that dropped it (#194).
    "drawer_id": "ckpt2",
    "text": "CHECKPOINT:2026-05-29|session:abc123|built.kind.filter",
    "wing": "palace_daemon",
    "room": "sessions",
}
_CONTENT_A = {
    "drawer_id": "c1",
    "text": "pgvector advisory lock race condition notes.",
    "topic": "problems",
    "wing": "palace_daemon",
    "room": "problems",
}
_CONTENT_B = {
    "drawer_id": "c2",
    "text": "The kind= filter classifies hits by topic or body prefix.",
    "wing": "palace_daemon",
    "room": "architecture",
}

_MIXED_PAYLOAD = {
    "query": "CHECKPOINT",
    "filters": {"wing": None, "room": None, "tags": None},
    "total_before_filter": 4,
    "results": [
        _CHECKPOINT_BY_TOPIC,
        _CONTENT_A,
        _CHECKPOINT_BY_BODY,
        _CONTENT_B,
    ],
}


def _drawer_ids(resp_json) -> list:
    return [h.get("drawer_id") for h in resp_json.get("results", [])]


def _checkpoint_count(resp_json) -> int:
    """Count CHECKPOINT: occurrences across hit bodies — mirrors the SME
    test's signal (context_string.count("CHECKPOINT:")), plus topic hits."""
    n = 0
    for h in resp_json.get("results", []):
        if search_routes._hit_is_checkpoint(h):
            n += 1
    return n


class TestHitIsCheckpoint(unittest.TestCase):
    """Unit-level: the two-signal checkpoint classifier."""

    def test_topic_checkpoint(self):
        self.assertTrue(search_routes._hit_is_checkpoint(_CHECKPOINT_BY_TOPIC))

    def test_topic_synonym_auto_save(self):
        self.assertTrue(
            search_routes._hit_is_checkpoint({"text": "x", "topic": "auto-save"})
        )

    def test_topic_case_insensitive(self):
        self.assertTrue(
            search_routes._hit_is_checkpoint({"text": "x", "topic": "  CheckPoint "})
        )

    def test_body_prefix_when_topic_missing(self):
        # The #194 metadata-loss case: no topic, but CHECKPOINT: body.
        self.assertTrue(search_routes._hit_is_checkpoint(_CHECKPOINT_BY_BODY))

    def test_nested_metadata_topic(self):
        self.assertTrue(
            search_routes._hit_is_checkpoint(
                {"text": "x", "metadata": {"topic": "checkpoint"}}
            )
        )

    def test_plain_content_is_not_checkpoint(self):
        self.assertFalse(search_routes._hit_is_checkpoint(_CONTENT_A))
        self.assertFalse(search_routes._hit_is_checkpoint(_CONTENT_B))

    def test_non_dict_is_not_checkpoint(self):
        self.assertFalse(search_routes._hit_is_checkpoint("nope"))


class TestApplyKindFilter(unittest.TestCase):
    """Unit-level: response filtering + filters-block echo."""

    def _fresh(self):
        return json.loads(json.dumps(_MIXED_PAYLOAD))  # deep copy

    def test_all_is_superset_unchanged(self):
        out = search_routes._apply_kind_filter(self._fresh(), "all")
        self.assertEqual(_drawer_ids(out), ["ckpt1", "c1", "ckpt2", "c2"])

    def test_content_excludes_checkpoints(self):
        out = search_routes._apply_kind_filter(self._fresh(), "content")
        self.assertEqual(_drawer_ids(out), ["c1", "c2"])
        self.assertEqual(_checkpoint_count(out), 0)

    def test_checkpoint_keeps_only_checkpoints(self):
        out = search_routes._apply_kind_filter(self._fresh(), "checkpoint")
        self.assertEqual(_drawer_ids(out), ["ckpt1", "ckpt2"])

    def test_content_strict_subset_of_all(self):
        all_out = search_routes._apply_kind_filter(self._fresh(), "all")
        content_out = search_routes._apply_kind_filter(self._fresh(), "content")
        all_ids = set(_drawer_ids(all_out))
        content_ids = set(_drawer_ids(content_out))
        self.assertTrue(content_ids < all_ids, "content must be a strict subset of all")

    def test_filters_block_echoes_kind(self):
        out = search_routes._apply_kind_filter(self._fresh(), "content")
        self.assertEqual(out["filters"]["kind"], "content")

    def test_non_dict_response_passthrough(self):
        self.assertEqual(search_routes._apply_kind_filter(["x"], "content"), ["x"])


class TestSearchKindFilterEndpoint(unittest.TestCase):
    """HTTP-level: /search and /context honour kind= end to end (#194)."""

    def setUp(self):
        # Empty API key disables auth; rerank off so we assert filtering only.
        self._env = patch.dict(
            os.environ,
            {"PALACE_API_KEY": "", "PALACE_RERANK_ENABLED": "false"},
            clear=False,
        )
        self._env.start()
        self.client = TestClient(main.app)

        async def _fake_call(request_dict, *a, **k):
            return _fake_mcp_envelope(json.loads(json.dumps(_MIXED_PAYLOAD)))

        self._call_patch = patch.object(main, "_call", new=AsyncMock(side_effect=_fake_call))
        self._call_patch.start()

    def tearDown(self):
        self._call_patch.stop()
        self._env.stop()

    def _get(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    def test_search_content_excludes_checkpoints(self):
        body = self._get("/search?q=CHECKPOINT&limit=20&kind=content&rerank=false")
        self.assertEqual(_drawer_ids(body), ["c1", "c2"])
        self.assertEqual(_checkpoint_count(body), 0)

    def test_search_all_includes_checkpoints(self):
        body = self._get("/search?q=CHECKPOINT&limit=20&kind=all&rerank=false")
        self.assertEqual(_checkpoint_count(body), 2)
        self.assertEqual(set(_drawer_ids(body)), {"ckpt1", "ckpt2", "c1", "c2"})

    def test_search_content_strict_subset_of_all(self):
        # The exact invariant the SME regression test asserts.
        all_body = self._get("/search?q=CHECKPOINT&limit=20&kind=all&rerank=false")
        content_body = self._get("/search?q=CHECKPOINT&limit=20&kind=content&rerank=false")
        n_all = _checkpoint_count(all_body)
        n_content = _checkpoint_count(content_body)
        self.assertGreater(n_all, n_content)
        self.assertEqual(n_content, 0)
        self.assertTrue(set(_drawer_ids(content_body)) < set(_drawer_ids(all_body)))

    def test_search_default_kind_is_content(self):
        # No kind= param → defaults to "content" → checkpoints excluded.
        body = self._get("/search?q=CHECKPOINT&limit=20&rerank=false")
        self.assertEqual(_checkpoint_count(body), 0)
        self.assertEqual(body["filters"]["kind"], "content")

    def test_search_checkpoint_kind_returns_only_checkpoints(self):
        body = self._get("/search?q=CHECKPOINT&limit=20&kind=checkpoint&rerank=false")
        self.assertEqual(set(_drawer_ids(body)), {"ckpt1", "ckpt2"})

    def test_search_bad_kind_400(self):
        resp = self.client.get("/search?q=x&kind=bogus")
        self.assertEqual(resp.status_code, 400, resp.text)
        self.assertIn("must be one of", resp.text)

    def test_context_content_excludes_checkpoints(self):
        body = self._get("/context?topic=CHECKPOINT&limit=20&kind=content")
        self.assertEqual(_drawer_ids(body), ["c1", "c2"])

    def test_context_bad_kind_400(self):
        resp = self.client.get("/context?topic=x&kind=bogus")
        self.assertEqual(resp.status_code, 400, resp.text)


if __name__ == "__main__":
    unittest.main()
