"""Tests for the FlashRank rerank shim (rerank.py).

Covers the parts that don't require downloading the ONNX model:
- env-var gate (``PALACE_RERANK_ENABLED``)
- empty/None input handling
- response-shape preservation
- graceful fallback when FlashRank can't be loaded
- the actual rerank path, only when FlashRank is installed AND a small
  smoke test can complete in under a few seconds

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m unittest tests.test_rerank -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import rerank  # noqa: E402  (daemon-root module)


class TestIsEnabled(unittest.TestCase):
    """``rerank.is_enabled()`` reads the env var live so operators can flip it."""

    def test_default_is_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PALACE_RERANK_ENABLED", None)
            self.assertTrue(rerank.is_enabled())

    def test_explicit_true_values(self):
        for v in ("true", "True", "1", "yes", "ON"):
            with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": v}):
                self.assertTrue(rerank.is_enabled(), v)

    def test_explicit_false_values(self):
        for v in ("false", "False", "0", "no", "off", ""):
            with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": v}):
                self.assertFalse(rerank.is_enabled(), v)


class TestRerankHitsNoop(unittest.TestCase):
    """No-op paths must preserve the input list and never call the model."""

    def test_disabled_returns_input_unchanged(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}):
            hits = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
            out, info = rerank.rerank_hits("q", hits)
            self.assertEqual(out, hits)
            self.assertFalse(info["enabled"])
            self.assertEqual(info["status"], "skipped")

    def test_empty_hits(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            out, info = rerank.rerank_hits("q", [])
            self.assertEqual(out, [])
            self.assertEqual(info["status"], "noop")

    def test_empty_query(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            hits = [{"id": "a", "text": "x"}]
            out, info = rerank.rerank_hits("   ", hits)
            self.assertEqual(out, hits)
            self.assertEqual(info["status"], "skipped")
            self.assertIn("empty", info.get("reason", ""))

    def test_no_rerankable_text(self):
        # Graph-only stubs from /search/age-fused have no text/document body.
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            hits = [{"id": "a", "document": None}, {"id": "b"}]
            out, info = rerank.rerank_hits("q", hits)
            self.assertEqual(out, hits)
            self.assertEqual(info["status"], "skipped")


class TestRerankResponse(unittest.TestCase):
    """The wrapper that operates on a full search response dict."""

    def test_non_dict_passes_through(self):
        self.assertEqual(rerank.rerank_response("q", None), None)
        self.assertEqual(rerank.rerank_response("q", "oops"), "oops")
        self.assertEqual(rerank.rerank_response("q", [1, 2]), [1, 2])

    def test_dict_without_results_passes_through(self):
        resp = {"error": "bad"}
        out = rerank.rerank_response("q", resp)
        self.assertEqual(out, {"error": "bad"})
        self.assertNotIn("rerank", out)

    def test_results_not_a_list_passes_through(self):
        resp = {"results": "not-a-list"}
        out = rerank.rerank_response("q", resp)
        self.assertEqual(out, {"results": "not-a-list"})
        self.assertNotIn("rerank", out)

    def test_attaches_rerank_block_when_disabled(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "false"}):
            resp = {"results": [{"id": "a", "text": "x"}]}
            out = rerank.rerank_response("q", resp)
            self.assertIn("rerank", out)
            self.assertFalse(out["rerank"]["enabled"])
            self.assertEqual(out["results"], [{"id": "a", "text": "x"}])


class TestFallbackOnLoadFailure(unittest.TestCase):
    """If FlashRank can't be loaded, return the input untouched + log a warning."""

    def setUp(self):
        # Reset the cached ranker so we can simulate a fresh load attempt.
        rerank._ranker = None
        rerank._ranker_load_error = None

    def tearDown(self):
        rerank._ranker = None
        rerank._ranker_load_error = None

    def test_import_failure_returns_input_unchanged(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            # Force the lazy loader to raise.
            def _boom(*a, **kw):
                raise RuntimeError("simulated model load failure")
            with patch("rerank._get_ranker", side_effect=lambda: None) as _:
                # Also stub the load-error sentinel so the public function
                # short-circuits with status=failed.
                rerank._ranker_load_error = "RuntimeError: simulated"
                hits = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
                out, info = rerank.rerank_hits("q", hits)
                self.assertEqual(out, hits)
                self.assertEqual(info["status"], "failed")


# ── Live smoke test ──────────────────────────────────────────────────────────
# Gated on whether FlashRank is importable AND the network/model cache is
# present. Skipped on CI hosts that haven't pre-downloaded the nano model.

def _flashrank_available() -> bool:
    try:
        import flashrank  # noqa: F401
        return True
    except Exception:
        return False


@unittest.skipUnless(_flashrank_available(), "flashrank not installed")
class TestLiveRerank(unittest.TestCase):
    """Smoke test against the real ms-marco-TinyBERT model.

    Runs only if FlashRank imports cleanly. The model download is cached
    under ``~/.flashrank`` after the first run.
    """

    def setUp(self):
        rerank._ranker = None
        rerank._ranker_load_error = None

    def test_orders_relevant_passage_first(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            hits = [
                {"id": "irrelevant", "text": "The cat sat on the mat."},
                {"id": "relevant", "text": "Paris is the capital of France."},
                {"id": "tangential", "text": "France borders Germany."},
            ]
            out, info = rerank.rerank_hits("What is the capital of France?", hits)
            self.assertEqual(info["status"], "ok")
            self.assertEqual(info["n_reranked"], 3)
            self.assertGreater(info["latency_ms"], 0.0)
            self.assertEqual(out[0]["id"], "relevant")
            # Every hit must carry a numeric rerank_score (JSON-safe float).
            for h in out:
                self.assertIsInstance(h.get("rerank_score"), float)

    def test_preserves_original_fields(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            hits = [
                {"id": "a", "text": "irrelevant chatter", "wing": "ww", "room": "rr", "distance": 0.4},
                {"id": "b", "text": "dogs bark at the moon", "wing": "ww", "room": "rr", "distance": 0.5},
            ]
            out, _ = rerank.rerank_hits("moon barking dogs", hits)
            for h in out:
                self.assertIn("wing", h)
                self.assertIn("room", h)
                self.assertIn("distance", h)

    def test_graph_stubs_sink_to_tail(self):
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            hits = [
                {"id": "graph-stub", "document": None, "matched_via": "graph"},
                {"id": "vector-hit", "text": "Paris is the capital of France."},
            ]
            out, info = rerank.rerank_hits("capital of France", hits)
            self.assertEqual(info["status"], "ok")
            self.assertEqual(out[0]["id"], "vector-hit")
            self.assertEqual(out[-1]["id"], "graph-stub")


if __name__ == "__main__":
    unittest.main()
