"""Unit tests for ``read_ontology_introspection`` — the GET /ontology reader.

Cat 8 (SME Ontology Coherence) introspection scored 0.0 because the daemon
could not surface its own declared-vs-effective ontology drift. This helper
adds that capability: it reports the documented MemPalace ontology
(``DECLARED_ONTOLOGY``), the live effective vocabulary read from Apache AGE
(populated relationship labels + MENTIONS ``etype`` distribution + counts), and
the reconciliation between them.

These tests pin the wiring without a live Postgres + AGE: they monkeypatch
``kg_reader._config``, stub the two AGE reads (``read_kg_postgres_stats`` and
``_read_effective_entity_kinds``), and assert the drift math. Same isolation
pattern as ``tests/test_graph_wings_dispatch.py`` (backend dispatch via a fake
``_Cfg``, no palace / postgres / chroma needed).

Run::

    cd /path/to/palace-daemon
    python -m unittest tests.test_ontology_introspection -v
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


class _Cfg:
    def __init__(self, backend):
        self.backend = backend


class TestDeclaredOntology(unittest.TestCase):
    def test_declared_mirrors_readme_claims(self):
        """The declared constant is the documented MemPalace ontology — the
        same 6 entity types / 3 edge types / 5 halls SME's
        implied_ontology_mempalace.yaml extracts. Pin it so the daemon's
        self-report can't silently drift from what SME measures against.
        """
        d = kg_reader.DECLARED_ONTOLOGY
        self.assertEqual(
            d["entity_types"],
            ["wing", "hall", "room", "drawer", "closet", "tunnel"],
        )
        self.assertEqual(d["edge_types"], ["hall", "tunnel", "member_of"])
        self.assertEqual(
            d["hall_vocabulary"],
            ["facts", "events", "discoveries", "preferences", "advice"],
        )
        self.assertEqual(d["structure"], "hierarchical")


class TestReadOntologyIntrospection(unittest.TestCase):
    def test_chroma_backend_returns_none(self):
        """Under chroma the AGE label tables don't exist; the reader declines
        rather than fabricate an empty ontology so /ontology can 503."""
        with patch.object(kg_reader, "_config", return_value=_Cfg("chroma")):
            self.assertIsNone(kg_reader.read_ontology_introspection())

    def test_age_unreachable_returns_none(self):
        """When the stats read returns None (AGE unreachable), the whole
        report degrades to None — the route turns that into a 503."""
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres_stats", return_value=None):
            self.assertIsNone(kg_reader.read_ontology_introspection())

    def test_current_corpus_drift_all_edges_absent(self):
        """The honest current-state report: MENTIONS dominates, RELATION is
        empty, and none of the declared edge types (hall/tunnel/member_of) are
        AGE relationship labels — so all three read as absent at the KG layer.
        That is exactly the over-claim drift Cat 8 should surface.
        """
        stats = {
            "entities": 267_519,
            "triples": 0,
            "mentions": 5_580_000,
            "relationship_types": ["MENTIONS"],
        }
        kinds = {"PROPER_NOUN": 12000, "TECH_IDENT": 7000, "UNTAGGED": 1000}
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres_stats", return_value=stats), \
             patch.object(kg_reader, "_read_effective_entity_kinds", return_value=kinds):
            report = kg_reader.read_ontology_introspection(sample_limit=20000)

        # Declared block carries the documented ontology verbatim.
        self.assertEqual(
            report["declared"]["entity_types"],
            ["wing", "hall", "room", "drawer", "closet", "tunnel"],
        )

        eff = report["effective"]
        self.assertEqual(eff["edge_types"], ["MENTIONS"])
        self.assertEqual(eff["entity_kinds"], kinds)
        self.assertEqual(eff["entities"], 267_519)
        self.assertEqual(eff["triples"], 0)
        self.assertEqual(eff["mentions"], 5_580_000)

        drift = report["drift"]
        # No declared edge type is a populated AGE label on this corpus.
        self.assertEqual(drift["declared_edge_types_present"], [])
        self.assertEqual(
            sorted(drift["declared_edge_types_absent"]),
            ["hall", "member_of", "tunnel"],
        )
        # Every effective etype is undeclared (none is a structural type).
        self.assertEqual(
            sorted(drift["entity_kinds_undeclared"]),
            ["PROPER_NOUN", "TECH_IDENT", "UNTAGGED"],
        )
        # 3/3 declared edges absent → drift_score 1.0.
        self.assertEqual(drift["drift_score"], 1.0)
        # Structure is not endorsed here — SME Cat 8e verifies it over /graph.
        self.assertEqual(drift["structure_claim"], "hierarchical")
        self.assertEqual(drift["structure_observed"], "not_computed")

    def test_relation_label_present_lowers_drift(self):
        """When the corpus gains a populated RELATION label (e.g. after a
        triple re-extraction), the reader does NOT credit it as a declared
        edge type — RELATION isn't in the declared vocabulary — so drift stays
        1.0 but the effective edge_types list reflects reality. This guards the
        case-insensitive match from spuriously matching unrelated labels.
        """
        stats = {
            "entities": 1000,
            "triples": 5000,
            "mentions": 10000,
            "relationship_types": ["RELATION", "MENTIONS"],
        }
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres_stats", return_value=stats), \
             patch.object(kg_reader, "_read_effective_entity_kinds", return_value={}):
            report = kg_reader.read_ontology_introspection()
        self.assertEqual(report["effective"]["edge_types"], ["RELATION", "MENTIONS"])
        self.assertEqual(report["drift"]["declared_edge_types_present"], [])
        self.assertEqual(report["drift"]["drift_score"], 1.0)

    def test_declared_edge_label_present_is_credited(self):
        """If a future re-extraction emits an AGE relationship label that
        matches a declared edge type (case-insensitively), it is credited as
        present and drift drops. This is the path Somnia's Cat 4/5 fix opens:
        adding real semantic edge types that the declared ontology names.
        """
        stats = {
            "entities": 1000,
            "triples": 5000,
            "mentions": 10000,
            # Hypothetical: extraction emits a 'hall'-labeled relationship.
            "relationship_types": ["HALL", "MENTIONS"],
        }
        with patch.object(kg_reader, "_config", return_value=_Cfg("postgres")), \
             patch.object(kg_reader, "read_kg_postgres_stats", return_value=stats), \
             patch.object(kg_reader, "_read_effective_entity_kinds", return_value={}):
            report = kg_reader.read_ontology_introspection()
        drift = report["drift"]
        self.assertEqual(drift["declared_edge_types_present"], ["hall"])
        self.assertEqual(sorted(drift["declared_edge_types_absent"]), ["member_of", "tunnel"])
        # 2/3 absent → 0.666...
        self.assertAlmostEqual(drift["drift_score"], 2 / 3, places=6)


if __name__ == "__main__":
    unittest.main()
