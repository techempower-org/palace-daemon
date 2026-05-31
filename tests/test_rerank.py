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


@unittest.skipUnless(_flashrank_available(), "flashrank not installed")
class TestRerankDeterminism(unittest.TestCase):
    """Reranking the SAME (query, candidates) must yield the SAME order +
    scores — within a process AND across a fresh model reload (a daemon
    restart). This pins the determinism guarantee that SME #117 relied on
    when it attributed a cross-restart top-5 reorder to the candidate-set
    change (the 2026-05-29 DB rebackfill), NOT to the reranker.

    If this ever fails, the rerank stage itself has become nondeterministic
    (e.g. an unstable sort, a float-tie reorder, or a nondeterministic ONNX
    provider) and a restart could silently reshuffle deployed results.
    """

    _QUERY = "What was my personal best time in the charity 5K run?"
    _CANDIDATES = [
        "I'm training for a charity 5K and want to beat my best of 25:50.",
        "Do you have drills to improve my tennis toss consistency?",
        "I set a new personal record of 24:32 at last weekend's 5K.",
        "Two-factor auth adds a second layer beyond your password.",
        "My best mile split during the 5K was 7:45.",
        "Recovery runs should stay in zone 2 heart rate.",
        "The charity raised $12,000 for the local food bank.",
        "I track my runs with a GPS watch and review the splits.",
    ]

    def setUp(self):
        rerank._ranker = None
        rerank._ranker_load_error = None

    def tearDown(self):
        rerank._ranker = None
        rerank._ranker_load_error = None

    def _rerank(self):
        hits = [{"id": i, "text": t} for i, t in enumerate(self._CANDIDATES)]
        with patch.dict(os.environ, {"PALACE_RERANK_ENABLED": "true"}):
            out, info = rerank.rerank_hits(self._QUERY, hits)
        self.assertEqual(info["status"], "ok")
        return [(h["id"], round(float(h["rerank_score"]), 9)) for h in out]

    def test_in_process_repeat_is_identical(self):
        """Same loaded ranker, reranked 4×: byte-identical order + scores."""
        first = self._rerank()
        for _ in range(3):
            self.assertEqual(self._rerank(), first)

    def test_fresh_reload_is_identical(self):
        """Force a brand-new model load (restart sim) between runs; identical."""
        first = self._rerank()
        # setUp/tearDown reset the singleton, but reload explicitly too so the
        # ONNX session + tokenizer are reconstructed from scratch.
        rerank._ranker = None
        rerank._ranker_load_error = None
        second = self._rerank()
        self.assertEqual(first, second)


class TestRerankPinHardening(unittest.TestCase):
    """Guard the determinism-across-deploys pins (docs/evals/2026-05-30-
    retrieval-determinism.md): the rerank stage is deterministic for a FIXED
    model, so the model must be frozen — an EXACT flashrank pin + an explicit
    PALACE_RERANK_MODEL in the systemd unit. A floor pin (``>=``) or a missing
    model env would let a fresh deploy silently reorder /search top-5.
    """

    def test_flashrank_is_exact_pinned(self):
        with open(os.path.join(_ROOT, "requirements.txt")) as fh:
            req = fh.read()
        lines = [
            ln.strip() for ln in req.splitlines()
            if ln.strip().lower().startswith("flashrank")
        ]
        self.assertEqual(len(lines), 1, f"expected one flashrank line, got {lines}")
        self.assertIn("==", lines[0],
                      f"flashrank must be EXACT-pinned (==), not a floor: {lines[0]!r}")
        self.assertNotIn(">=", lines[0])

    def test_systemd_unit_pins_rerank_model(self):
        with open(os.path.join(_ROOT, "palace-daemon.service")) as fh:
            unit = fh.read()
        self.assertIn("PALACE_RERANK_MODEL=ms-marco-TinyBERT-L-2-v2", unit,
                      "palace-daemon.service must pin PALACE_RERANK_MODEL "
                      "to the deployed default")


if __name__ == "__main__":
    unittest.main()
