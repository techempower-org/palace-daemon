"""Tests for the novelty calibration harness (scripts/calibrate_novelty.py).

Covers the pure analysis helpers (summarize, histogram) and the offline
synthetic pipeline end to end — the live-daemon path is exercised manually
against familiar:8085 and isn't unit-tested here (it requires the daemon).

Run with::

    venv/bin/python -m pytest tests/test_calibrate_novelty.py -q
"""
import importlib.util
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_SCRIPT = os.path.join(_ROOT, "scripts", "calibrate_novelty.py")
_spec = importlib.util.spec_from_file_location("calibrate_novelty", _SCRIPT)
cal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cal)


class TestSummarize(unittest.TestCase):

    def test_empty(self):
        self.assertEqual(cal.summarize([]), {"n": 0})

    def test_percentiles_monotonic(self):
        st = cal.summarize([i / 100 for i in range(101)])
        self.assertEqual(st["n"], 101)
        self.assertLessEqual(st["p05"], st["p10"])
        self.assertLessEqual(st["p10"], st["median"])
        self.assertLessEqual(st["median"], st["p90"])
        self.assertLessEqual(st["p90"], st["p95"])
        self.assertAlmostEqual(st["median"], 0.5, delta=0.02)

    def test_min_max(self):
        st = cal.summarize([0.2, 0.8, 0.5])
        self.assertEqual(st["min"], 0.2)
        self.assertEqual(st["max"], 0.8)


class TestHistogram(unittest.TestCase):

    def test_bins_count_total(self):
        scores = [0.05, 0.15, 0.15, 0.95]
        bins = cal._hist_data(scores, 20)
        self.assertEqual(len(bins), 20)
        self.assertEqual(sum(b["count"] for b in bins), len(scores))

    def test_score_one_lands_in_last_bin(self):
        bins = cal._hist_data([1.0], 20)
        self.assertEqual(bins[-1]["count"], 1)

    def test_ascii_no_scores(self):
        self.assertIn("no scores", cal.ascii_histogram([]))

    def test_out_of_range_scores_clamped_hist_data(self):
        # NCD can slightly exceed 1.0; a negative score must not wrap into a
        # high bin via Python's negative indexing. Both extremes are clamped.
        bins = cal._hist_data([-0.3, 1.4], 20)
        self.assertEqual(sum(b["count"] for b in bins), 2)
        self.assertEqual(bins[0]["count"], 1)   # -0.3 clamps to first bin
        self.assertEqual(bins[-1]["count"], 1)  # 1.4 clamps to last bin

    def test_out_of_range_scores_clamped_ascii(self):
        # Must not raise IndexError on out-of-range scores.
        out = cal.ascii_histogram([-0.5, 1.2, 0.5], bins=20)
        self.assertIn("[0.00-0.05)", out)


class TestSyntheticCorpus(unittest.TestCase):

    def test_every_group_has_enough_for_window(self):
        import random
        corpus = cal.build_synthetic_corpus(random.Random(1), window=20, per_group=8)
        self.assertTrue(corpus)
        for (wing, room), contents in corpus.items():
            self.assertGreater(len(contents), 20, f"{wing}/{room} too small")

    def test_distinct_groups(self):
        import random
        corpus = cal.build_synthetic_corpus(random.Random(1), window=10, per_group=4)
        # 7 wings x 7 rooms = 49 distinct (wing, room) groups
        self.assertEqual(len(corpus), 49)


class TestOfflineRun(unittest.TestCase):

    def _args(self, **kw):
        import argparse
        ns = argparse.Namespace(
            window=20, per_group=10, seed=47, use_preview=False, out=None,
        )
        for k, v in kw.items():
            setattr(ns, k, v)
        return ns

    def test_run_offline_produces_bimodal_scores(self):
        result = cal.run_offline(self._args())
        self.assertGreater(result["overall"]["n"], 100)
        # The synthetic corpus mixes near-duplicate and varied groups, so the
        # spread should be wide (low cluster well below the high cluster).
        self.assertLess(result["overall"]["p10"], 0.35)
        self.assertGreater(result["overall"]["p90"], 0.40)
        self.assertGreater(result["overall"]["p90"] - result["overall"]["p10"], 0.15)

    def test_run_offline_records_have_status_ok(self):
        result = cal.run_offline(self._args())
        ok = [r for r in result["records"] if r["status"] == "ok"]
        self.assertEqual(len(ok), len(result["records"]))
        for r in ok:
            self.assertIsInstance(r["novelty_score"], float)
            self.assertGreaterEqual(r["novelty_score"], 0.0)
            self.assertLessEqual(r["novelty_score"], 1.5)

    def test_writes_json_out(self):
        import json
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "evals", "x.json")
            cal.run_offline(self._args(out=out))
            self.assertTrue(os.path.exists(out))
            with open(out) as f:
                data = json.load(f)
            self.assertIn("overall", data)
            self.assertIn("histogram_bins", data)
            self.assertEqual(data["config"]["mode"], "offline-synthetic")


class TestMcpCallRobustness(unittest.TestCase):
    """_mcp_call must not crash on non-JSON daemon responses (502/504 HTML,
    empty body) — it should degrade to an empty result so the calibration run
    survives a transient bad response."""

    class _FakeResp:
        def __init__(self, body):
            self._body = body.encode("utf-8")

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _call_with_body(self, body):
        from unittest.mock import patch
        with patch.object(cal.urllib.request, "urlopen",
                          return_value=self._FakeResp(body)):
            return cal._mcp_call("http://x", "k", "tool", {})

    def test_html_error_page_returns_empty(self):
        self.assertEqual(self._call_with_body("<html>502 Bad Gateway</html>"), {})

    def test_empty_body_returns_empty(self):
        self.assertEqual(self._call_with_body(""), {})

    def test_valid_outer_without_content_returns_outer(self):
        # Parsable JSON but not the tools/call envelope — return it as-is.
        out = self._call_with_body('{"some": "thing"}')
        self.assertEqual(out, {"some": "thing"})

    def test_full_envelope_unwrapped(self):
        body = ('{"result": {"content": [{"type": "text", '
                '"text": "{\\"drawers\\": []}"}]}}')
        self.assertEqual(self._call_with_body(body), {"drawers": []})


if __name__ == "__main__":
    unittest.main()
