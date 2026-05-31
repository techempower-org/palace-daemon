"""Route tests for GET/POST /graph/structural-stats.

The heavy full-graph Cat 5/8 compute (WCC + Louvain over ~1.9M edges) must
never run inline on demand — it's a GB-RAM/minutes spike on the box serving the
live palace. These tests pin the gating + caching contract without running the
real compute (``_read_structural_stats`` is mocked):

  * POST is REFUSED 409 while a bench is active (.bench-active.lock).
  * POST computes once + caches; GET returns the cache.
  * GET is 404 before any compute (never triggers the compute itself).
  * None from the reader (chroma / unreachable AGE) → 503.

Run::

    cd palace-daemon
    python -m unittest tests.test_structural_stats_route -v
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


_FAKE_STATS = {
    "entities": 1156241,
    "edges": 1921600,
    "component_count": 50000,
    "largest_component_size": 800000,
    "largest_component_fraction": 0.69,
    "isolate_count": 12000,
    "component_size_histogram": [800000, 1200, 900],
    "modularity": 0.72,
    "modularity_communities": 41,
    "modularity_note": None,
}


class TestStructuralStatsRoute(unittest.TestCase):
    def setUp(self):
        # Empty key → _check_auth / _check_viz_auth are no-ops.
        self._env = patch.dict(os.environ, {"PALACE_API_KEY": ""}, clear=False)
        self._env.start()
        self.client = TestClient(main.app)
        # Reset the module cache between tests.
        main._STRUCTURAL_STATS_CACHE = None

    def tearDown(self):
        self._env.stop()
        main._STRUCTURAL_STATS_CACHE = None

    def test_get_404_before_compute(self):
        """GET returns 404 (not a compute) until POST populates the cache —
        a plain read can never trigger the heavy run."""
        with patch.object(main, "_read_structural_stats") as reader:
            resp = self.client.get("/graph/structural-stats")
            self.assertEqual(resp.status_code, 404)
            reader.assert_not_called()  # GET must never compute

    def test_post_refused_409_while_bench_active(self):
        """The heavy compute is refused while a bench is running, so it can't
        contend with a bench for the box's RAM/CPU."""
        with patch.object(main, "_bench_lock_active", return_value=(True, "bench.lock present")), \
             patch.object(main, "_read_structural_stats") as reader:
            resp = self.client.post("/graph/structural-stats")
        self.assertEqual(resp.status_code, 409)
        reader.assert_not_called()  # never computed under an active bench

    def test_post_computes_and_caches_then_get_serves(self):
        with patch.object(main, "_bench_lock_active", return_value=(False, "")), \
             patch.object(main, "_read_structural_stats", return_value=_FAKE_STATS) as reader:
            post = self.client.post("/graph/structural-stats")
            self.assertEqual(post.status_code, 200)
            self.assertEqual(post.json()["modularity"], 0.72)
            reader.assert_called_once()
            # GET now serves the cached value WITHOUT recomputing.
            with patch.object(main, "_read_structural_stats") as reader2:
                get = self.client.get("/graph/structural-stats")
                self.assertEqual(get.status_code, 200)
                self.assertEqual(get.json()["largest_component_size"], 800000)
                reader2.assert_not_called()

    def test_post_503_when_reader_returns_none(self):
        """chroma backend / unreachable AGE → reader None → 503, cache stays
        empty (no bogus payload cached)."""
        with patch.object(main, "_bench_lock_active", return_value=(False, "")), \
             patch.object(main, "_read_structural_stats", return_value=None):
            resp = self.client.post("/graph/structural-stats")
        self.assertEqual(resp.status_code, 503)
        self.assertIsNone(main._STRUCTURAL_STATS_CACHE)

    def test_post_with_modularity_false_threads_through(self):
        with patch.object(main, "_bench_lock_active", return_value=(False, "")), \
             patch.object(main, "_read_structural_stats", return_value=_FAKE_STATS) as reader:
            self.client.post("/graph/structural-stats?with_modularity=false")
            _, kwargs = reader.call_args
            self.assertEqual(kwargs.get("with_modularity"), False)


if __name__ == "__main__":
    unittest.main()
