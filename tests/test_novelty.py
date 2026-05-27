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
import json
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

    def test_scoring_reads_content_preview_field(self):
        """Regression: mempalace_list_drawers returns drawer bodies under
        ``content_preview``, not ``text``/``content``. If the fallback chain
        in compute_novelty_for_write omits it, the window is empty and every
        write scores 1.0 ("no_window") — the feature is a silent no-op (the
        state it shipped in at #45). This mock uses the REAL field name so the
        seam stays covered."""
        mock_call = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"drawers": [{"content_preview": "existing drawer about NCD compression scoring"}, {"content_preview": "another drawer about gzip distance metrics"}]}'}]
            },
        })
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "true"}):
            info = self._run(
                novelty.compute_novelty_for_write(
                    "existing drawer about NCD compression scoring",
                    "wing_test", "discoveries", mock_call,
                )
            )
        # Window populated from content_preview → real scoring, not no_window.
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["window_size"], 2)
        # Content matches a window entry, so it must score as low-novelty.
        self.assertLess(info["novelty_score"], 0.5)

    def test_malformed_drawer_entry_is_skipped_not_swallowed(self):
        """Regression: a non-dict drawer item must not raise inside the loop.
        If it did, the outer ``except Exception`` would silently fall back to
        novelty_score=1.0 — re-creating the no-op #63 fixed. The bad entry is
        skipped; the well-formed sibling still populates the window."""
        mock_call = AsyncMock(return_value={
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "content": [{"type": "text", "text": '{"drawers": ["i am a string not a dict", null, {"content_preview": "real neighbour drawer about gzip ncd scoring"}]}'}]
            },
        })
        with patch.dict(os.environ, {"PALACE_NOVELTY_ENABLED": "true"}):
            info = self._run(
                novelty.compute_novelty_for_write(
                    "real neighbour drawer about gzip ncd scoring",
                    "wing_test", "discoveries", mock_call,
                )
            )
        # Must reach real scoring (status ok), NOT the failure fallback.
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["window_size"], 1)
        self.assertLess(info["novelty_score"], 0.5)

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


def _envelope(payload: dict) -> dict:
    """Wrap a dict as an MCP tools/call result envelope."""
    return {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }


def _dispatching_call(list_payload: dict, full_by_id: dict[str, str]):
    """Build an async call_fn that routes by tool name.

    list_drawers -> ``list_payload``; get_drawer -> the full content registered
    in ``full_by_id`` for that drawer_id (or a no-content envelope if missing).
    Records call counts on the returned function for assertions.
    """
    counts = {"list_drawers": 0, "get_drawer": 0}

    async def call_fn(req):
        name = req["params"]["name"]
        if name == "mempalace_list_drawers":
            counts["list_drawers"] += 1
            return _envelope(list_payload)
        if name == "mempalace_get_drawer":
            counts["get_drawer"] += 1
            did = req["params"]["arguments"]["drawer_id"]
            content = full_by_id.get(did)
            if content is None:
                return _envelope({"drawer_id": did})  # no usable content
            return _envelope({"drawer_id": did, "content": content})
        raise AssertionError(f"unexpected tool {name}")

    call_fn.counts = counts
    return call_fn


class TestFullContentScoring(unittest.TestCase):
    """#65: score against full neighbour content, not truncated preview."""

    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def _clean_env(self, **overrides):
        # Start from a known state: enabled, defaults for the new knobs.
        env = {"PALACE_NOVELTY_ENABLED": "true"}
        env.update(overrides)
        return patch.dict(os.environ, env, clear=False)

    def test_full_content_path_fetches_per_member(self):
        # Preview is truncated; full content is the real body. With the default
        # knob on, scoring uses full content and a get_drawer fires per member.
        list_payload = {"drawers": [
            {"drawer_id": "d1", "content_preview": "alpha beta"},
            {"drawer_id": "d2", "content_preview": "gamma delta"},
        ]}
        full = {
            "d1": "alpha beta " * 30,  # long full bodies, distinct from preview
            "d2": "gamma delta " * 30,
        }
        call_fn = _dispatching_call(list_payload, full)
        for k in ("PALACE_NOVELTY_FULL_CONTENT", "PALACE_NOVELTY_FULL_CONTENT_WINDOW"):
            os.environ.pop(k, None)
        with self._clean_env():
            info = self._run(novelty.compute_novelty_for_write(
                "alpha beta " * 30, "wing_test", "discoveries", call_fn))
        self.assertEqual(info["status"], "ok")
        self.assertTrue(info["full_content"])
        self.assertEqual(info["full_content_used"], 2)
        self.assertEqual(call_fn.counts["list_drawers"], 1)
        self.assertEqual(call_fn.counts["get_drawer"], 2)
        # Identical full body present → near-zero novelty.
        self.assertLess(info["novelty_score"], 0.1)

    def test_knob_off_uses_preview_only(self):
        list_payload = {"drawers": [
            {"drawer_id": "d1", "content_preview": "alpha beta gamma delta"},
        ]}
        call_fn = _dispatching_call(list_payload, {"d1": "should not be fetched"})
        with self._clean_env(PALACE_NOVELTY_FULL_CONTENT="false"):
            info = self._run(novelty.compute_novelty_for_write(
                "alpha beta gamma delta", "wing_test", "discoveries", call_fn))
        self.assertEqual(info["status"], "ok")
        self.assertFalse(info["full_content"])
        self.assertEqual(info["full_content_used"], 0)
        self.assertEqual(call_fn.counts["get_drawer"], 0)  # no fetches at all

    def test_fetch_failure_falls_back_to_preview(self):
        # get_drawer returns no content for d1 → fall back to its preview, but
        # scoring still succeeds (no failure status).
        list_payload = {"drawers": [
            {"drawer_id": "d1", "content_preview": "real neighbour about gzip ncd"},
        ]}
        call_fn = _dispatching_call(list_payload, {})  # no full content registered
        for k in ("PALACE_NOVELTY_FULL_CONTENT", "PALACE_NOVELTY_FULL_CONTENT_WINDOW"):
            os.environ.pop(k, None)
        with self._clean_env():
            info = self._run(novelty.compute_novelty_for_write(
                "real neighbour about gzip ncd", "wing_test", "discoveries", call_fn))
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["window_size"], 1)
        self.assertEqual(info["full_content_used"], 0)  # fetch yielded nothing
        self.assertEqual(call_fn.counts["get_drawer"], 1)  # but it was attempted
        self.assertLess(info["novelty_score"], 0.5)

    def test_full_content_window_caps_fetches(self):
        # 3 members but the cap is 1 → only one get_drawer; the rest use preview.
        list_payload = {"drawers": [
            {"drawer_id": "d1", "content_preview": "p1"},
            {"drawer_id": "d2", "content_preview": "p2"},
            {"drawer_id": "d3", "content_preview": "p3"},
        ]}
        call_fn = _dispatching_call(list_payload, {
            "d1": "f1 " * 20, "d2": "f2 " * 20, "d3": "f3 " * 20})
        with self._clean_env(PALACE_NOVELTY_FULL_CONTENT_WINDOW="1"):
            info = self._run(novelty.compute_novelty_for_write(
                "brand new content", "wing_test", "discoveries", call_fn))
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["window_size"], 3)
        self.assertEqual(info["full_content_used"], 1)
        self.assertEqual(call_fn.counts["get_drawer"], 1)

    def test_full_content_window_zero_disables_fetches(self):
        list_payload = {"drawers": [
            {"drawer_id": "d1", "content_preview": "p1"},
        ]}
        call_fn = _dispatching_call(list_payload, {"d1": "f1 " * 20})
        with self._clean_env(PALACE_NOVELTY_FULL_CONTENT_WINDOW="0"):
            info = self._run(novelty.compute_novelty_for_write(
                "brand new content", "wing_test", "discoveries", call_fn))
        self.assertEqual(info["status"], "ok")
        self.assertEqual(info["full_content_used"], 0)
        self.assertEqual(call_fn.counts["get_drawer"], 0)


class TestConfigKnobs(unittest.TestCase):

    def test_full_content_default_true(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PALACE_NOVELTY_FULL_CONTENT", None)
            self.assertTrue(novelty._full_content_enabled())

    def test_full_content_false_values(self):
        for v in ("false", "0", "no", "off"):
            with patch.dict(os.environ, {"PALACE_NOVELTY_FULL_CONTENT": v}):
                self.assertFalse(novelty._full_content_enabled(), v)

    def test_full_content_window_defaults_to_window(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_WINDOW": "15"}):
            os.environ.pop("PALACE_NOVELTY_FULL_CONTENT_WINDOW", None)
            self.assertEqual(novelty._full_content_window(), 15)

    def test_full_content_window_explicit(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_FULL_CONTENT_WINDOW": "5"}):
            self.assertEqual(novelty._full_content_window(), 5)

    def test_full_content_window_invalid_falls_back(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_FULL_CONTENT_WINDOW": "nope",
                                     "PALACE_NOVELTY_WINDOW": "20"}):
            self.assertEqual(novelty._full_content_window(), 20)

    def test_full_content_window_negative_clamps_zero(self):
        with patch.dict(os.environ, {"PALACE_NOVELTY_FULL_CONTENT_WINDOW": "-3"}):
            self.assertEqual(novelty._full_content_window(), 0)


if __name__ == "__main__":
    unittest.main()
