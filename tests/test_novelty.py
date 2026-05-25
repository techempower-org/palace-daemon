"""Tests for gzip-NCD novelty scoring (novelty.py).

Covers:
- NCD computation correctness (identical, disjoint, partial overlap)
- env-var gate (PALACE_NOVELTY_ENABLED)
- window size configuration
- edge cases (empty strings, empty window)
- score_novelty integration
- async compute_novelty_for_write with mocked _call

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m unittest tests.test_novelty -v
"""
import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import novelty  # noqa: E402


class TestNCD(unittest.TestCase):
    """Core NCD function — pure computation, no I/O."""

    def test_identical_strings(self):
        # Short strings have gzip header overhead; use a longer string
        # for a tighter bound, or accept ~0.1 delta for short ones.
        d = novelty.ncd("hello world " * 20, "hello world " * 20)
        self.assertAlmostEqual(d, 0.0, delta=0.1)

    def test_very_similar(self):
        a = "The palace daemon manages memory storage efficiently"
        b = "The palace daemon manages memory storage very efficiently"
        d = novelty.ncd(a, b)
        self.assertLess(d, 0.5)

    def test_disjoint_strings(self):
        # NCD on truly disjoint content; repeated substrings compress well
        # so we use varied vocabulary. NCD > 0.4 is reliable for unrelated
        # content; the 0.7+ range needs multi-paragraph inputs.
        a = "quantum entanglement photon wavelength spectroscopy laser optics diffraction"
        b = "chocolate cake recipe vanilla frosting baking soda flour sugar butter eggs"
        d = novelty.ncd(a, b)
        self.assertGreater(d, 0.3)

    def test_empty_both(self):
        self.assertEqual(novelty.ncd("", ""), 0.0)

    def test_empty_one(self):
        self.assertEqual(novelty.ncd("hello", ""), 1.0)
        self.assertEqual(novelty.ncd("", "hello"), 1.0)

    def test_approximate_symmetry(self):
        # NCD is only approximately symmetric — gzip's LZ77 builds its
        # dictionary left-to-right, so compress(a+b) != compress(b+a).
        # The asymmetry shrinks with longer inputs.
        a = "palace daemon deployment and configuration management " * 10
        b = "mempalace chromadb backend storage infrastructure ops " * 10
        self.assertAlmostEqual(novelty.ncd(a, b), novelty.ncd(b, a), delta=0.05)

    def test_returns_float(self):
        d = novelty.ncd("foo", "bar")
        self.assertIsInstance(d, float)

    def test_range_bounded(self):
        d = novelty.ncd("abc" * 100, "xyz" * 100)
        self.assertGreaterEqual(d, 0.0)
        self.assertLessEqual(d, 1.5)  # NCD can slightly exceed 1.0 in practice


class TestIsEnabled(unittest.TestCase):

    def test_default_is_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PALACE_NOVELTY_ENABLED", None)
            self.assertTrue(novelty.is_enabled())

    def test_explicit_true_values(self):
        for v in ("true", "True", "1", "yes", "ON"):
            with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": v}):
                self.assertTrue(novelty.is_enabled(), v)

    def test_false_values(self):
        for v in ("false", "0", "no", "off", "nope"):
            with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": v}):
                self.assertFalse(novelty.is_enabled(), v)


class TestWindowSize(unittest.TestCase):

    def test_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PALACE_NOVELTY_WINDOW", None)
            self.assertEqual(novelty._window_size(), 20)

    def test_custom(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_WINDOW": "50"}):
            self.assertEqual(novelty._window_size(), 50)

    def test_invalid_falls_back(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_WINDOW": "not_a_number"}):
            self.assertEqual(novelty._window_size(), 20)

    def test_zero_clamps_to_one(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_WINDOW": "0"}):
            self.assertEqual(novelty._window_size(), 1)


class TestScoreNovelty(unittest.TestCase):

    def test_disabled(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "false"}):
            info = novelty.score_novelty("new content", ["old content"])
            self.assertEqual(info["status"], "skipped")
            self.assertFalse(info["enabled"])

    def test_empty_content(self):
        info = novelty.score_novelty("", ["old content"])
        self.assertEqual(info["status"], "skipped")

    def test_empty_window(self):
        info = novelty.score_novelty("new content", [])
        self.assertEqual(info["status"], "no_window")
        self.assertEqual(info["novelty_score"], 1.0)

    def test_duplicate_content(self):
        existing = ["This is existing drawer content about palace daemon"]
        info = novelty.score_novelty(
            "This is existing drawer content about palace daemon",
            existing,
        )
        self.assertEqual(info["status"], "ok")
        self.assertLess(info["novelty_score"], 0.1)
        self.assertEqual(info["most_similar_index"], 0)

    def test_novel_content(self):
        existing = [
            "Palace daemon crash-loop detection and recovery",
            "ChromaDB HNSW segment health monitoring",
        ]
        info = novelty.score_novelty(
            "Kubernetes pod autoscaling with HPA metrics",
            existing,
        )
        self.assertEqual(info["status"], "ok")
        self.assertGreater(info["novelty_score"], 0.5)

    def test_finds_most_similar(self):
        existing = [
            "Unrelated content about cooking recipes",
            "Palace daemon configuration and deployment",
            "Unrelated content about gardening tips",
        ]
        info = novelty.score_novelty(
            "Palace daemon setup and deploy process",
            existing,
        )
        self.assertEqual(info["most_similar_index"], 1)

    def test_window_size_field(self):
        existing = ["a", "b", "c"]
        info = novelty.score_novelty("d", existing)
        self.assertEqual(info["window_size"], 3)

    def test_skips_empty_texts_in_window(self):
        existing = ["", "", "real content about testing"]
        info = novelty.score_novelty("real content about testing procedures", existing)
        self.assertEqual(info["status"], "ok")
        self.assertLess(info["novelty_score"], 0.5)


class TestComputeNoveltyForWrite(unittest.TestCase):

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_disabled_skips_call(self):
        mock_call = AsyncMock()
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "false"}):
            info = self._run(
                novelty.compute_novelty_for_write("content", "wing_test", "decisions", mock_call)
            )
        self.assertFalse(info["enabled"])
        mock_call.assert_not_called()

    def test_successful_scoring(self):
        mock_call = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"drawers": [{"text": "existing drawer about NCD"}, {"text": "another drawer about compression"}]}'}]
            },
        })
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "true"}):
            info = self._run(
                novelty.compute_novelty_for_write(
                    "brand new topic about quantum computing",
                    "wing_test", "discoveries", mock_call,
                )
            )
        self.assertEqual(info["status"], "ok")
        self.assertIn("novelty_score", info)
        self.assertIsInstance(info["novelty_score"], float)
        mock_call.assert_called_once()

    def test_call_failure_returns_default(self):
        mock_call = AsyncMock(side_effect=Exception("connection refused"))
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "true"}):
            info = self._run(
                novelty.compute_novelty_for_write("content", "wing_test", "sessions", mock_call)
            )
        self.assertEqual(info["status"], "failed")
        self.assertEqual(info["novelty_score"], 1.0)

    def test_empty_drawers_response(self):
        mock_call = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"drawers": []}'}]
            },
        })
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "true"}):
            info = self._run(
                novelty.compute_novelty_for_write("content", "wing_test", "sessions", mock_call)
            )
        self.assertEqual(info["status"], "no_window")


if __name__ == "__main__":
    unittest.main()
