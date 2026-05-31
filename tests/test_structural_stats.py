"""Unit tests for full-graph Cat 5/8 structural stats (kg_reader).

SME Cat 5 (connectivity/isolates) and Cat 8 (modularity) need the EXACT
full-graph reading, not a biased /graph sample (the `other` fraction alone
drifts 47%→58% across sample sizes). ``compute_structural_stats`` is the pure
WCC + Louvain core — these tests pin it against synthetic graphs with known
answers, no DB. The DB read (``read_structural_stats``) is a thin shim around
it, exercised by the dispatch test.

Run::

    cd /path/to/palace-daemon
    python -m unittest tests.test_structural_stats -v
"""
import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import kg_reader  # noqa: E402

try:
    import networkx  # noqa: F401
    _HAS_NX = True
except ImportError:
    _HAS_NX = False


class _Cfg:
    def __init__(self, backend):
        self.backend = backend


class TestComputeStructuralStats(unittest.TestCase):
    def test_two_disjoint_triangles(self):
        """Two disjoint triangles over 6 entities → 2 components, largest 3,
        no isolates, modularity > 0 (clear two-community split)."""
        edges = [(1, 2), (2, 3), (3, 1), (4, 5), (5, 6), (6, 4)]
        s = kg_reader.compute_structural_stats(edges, total_entities=6)
        self.assertEqual(s["edges"], 6)
        self.assertEqual(s["component_count"], 2)
        self.assertEqual(s["largest_component_size"], 3)
        self.assertEqual(s["isolate_count"], 0)
        self.assertEqual(s["largest_component_fraction"], 0.5)
        self.assertEqual(s["component_size_histogram"], [3, 3])

    def test_isolates_counted_against_full_entity_universe(self):
        """Entities present but in NO edge are isolates — counted against the
        full Entity count, not just edge endpoints. One edge over 2 of 10
        entities → 8 isolates, component_count = 1 + 8 singletons."""
        edges = [(1, 2)]
        s = kg_reader.compute_structural_stats(edges, total_entities=10)
        self.assertEqual(s["edges"], 1)
        self.assertEqual(s["isolate_count"], 8)
        self.assertEqual(s["largest_component_size"], 2)
        # 1 real component + 8 edge-less singletons.
        self.assertEqual(s["component_count"], 9)
        self.assertAlmostEqual(s["largest_component_fraction"], 0.2)

    @unittest.skipUnless(_HAS_NX, "networkx not installed")
    def test_modularity_high_for_clustered_graph(self):
        """Two 5-cliques bridged by one edge → Louvain finds 2 communities,
        modularity well above 0 (textbook 2-community structure)."""
        clique_a = [(i, j) for i in range(5) for j in range(i + 1, 5)]
        clique_b = [(i, j) for i in range(10, 15) for j in range(i + 1, 15)]
        bridge = [(0, 10)]
        edges = clique_a + clique_b + bridge
        s = kg_reader.compute_structural_stats(edges, total_entities=10)
        self.assertIsNotNone(s["modularity"])
        self.assertGreater(s["modularity"], 0.3)
        self.assertGreaterEqual(s["modularity_communities"], 2)

    def test_modularity_note_when_networkx_missing(self):
        """If networkx is unavailable, connectivity stats are still exact and
        modularity degrades to None with an explanatory note (the endpoint
        ships Cat 5 with zero new dependency)."""
        import builtins
        real_import = builtins.__import__

        def _no_networkx(name, *a, **k):
            if name == "networkx" or name.startswith("networkx."):
                raise ImportError("networkx blocked for test")
            return real_import(name, *a, **k)

        with patch.object(builtins, "__import__", _no_networkx):
            s = kg_reader.compute_structural_stats([(1, 2), (2, 3)], total_entities=3)
        self.assertIsNone(s["modularity"])
        self.assertIn("networkx not installed", s["modularity_note"])
        # Connectivity (Cat 5) is unaffected — exact, dependency-free.
        self.assertEqual(s["largest_component_size"], 3)
        self.assertEqual(s["isolate_count"], 0)

    def test_with_modularity_false_skips_louvain(self):
        """Connectivity-only mode returns None modularity (cheap path)."""
        edges = [(1, 2), (2, 3)]
        s = kg_reader.compute_structural_stats(
            edges, total_entities=3, with_modularity=False
        )
        self.assertIsNone(s["modularity"])
        self.assertEqual(s["largest_component_size"], 3)

    def test_empty_graph(self):
        """No edges, N entities → all isolates, no modularity, no crash."""
        s = kg_reader.compute_structural_stats([], total_entities=5)
        self.assertEqual(s["edges"], 0)
        self.assertEqual(s["isolate_count"], 5)
        self.assertEqual(s["component_count"], 5)
        self.assertIsNone(s["modularity"])

    def test_none_endpoints_skipped(self):
        """Malformed rows (None endpoint) are skipped, not crashed on."""
        edges = [(1, 2), (None, 3), (4, None)]
        s = kg_reader.compute_structural_stats(edges, total_entities=4)
        self.assertEqual(s["edges"], 1)


class TestReadStructuralStatsDispatch(unittest.TestCase):
    def test_chroma_backend_returns_none(self):
        with patch.object(kg_reader, "_config", return_value=_Cfg("chroma")):
            self.assertIsNone(kg_reader.read_structural_stats())

    def test_postgres_no_dsn_returns_none(self):
        cfg = _Cfg("postgres")
        cfg.postgres_dsn = None
        with patch.object(kg_reader, "_config", return_value=cfg), \
             patch.dict(os.environ, {"MEMPALACE_POSTGRES_DSN": ""}, clear=False):
            self.assertIsNone(kg_reader.read_structural_stats())


if __name__ == "__main__":
    unittest.main()
