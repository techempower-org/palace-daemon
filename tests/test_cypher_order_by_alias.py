"""Regression tests for issue #209 — RETURN/WITH aliases in ORDER BY.

Apache AGE resolves ``ORDER BY`` identifiers against the range table of the
clauses *preceding* the projection, not against the projection's own alias
list. So a query that orders by an alias it just introduced::

    MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n
    ORDER BY n DESC

fails with ``could not find rte for n``, while ordering by the underlying
expression (``ORDER BY count(*) DESC``) succeeds.

Measured against production AGE (mempalace_kg) on 2026-09-10, zero-scan
UNWIND probes:

    RETURN x AS k ORDER BY k                     -> 400 "could not find rte for k"
    RETURN x AS k, count(*) AS n ORDER BY n DESC -> 400 "could not find rte for n"
    RETURN x AS k, count(*) AS n ORDER BY (count(*)) DESC -> 200 OK
    WITH x AS k ORDER BY k                       -> 400 "could not find rte for k"
    WITH x AS k ORDER BY (x)                     -> 200 OK

Note the bug is NOT aggregate-specific (the issue text says it is) — a plain
non-aggregate alias fails identically.

The daemon therefore rewrites alias references in ORDER BY back into the
expression that defined them before handing the Cypher to AGE.
"""
import asyncio
import os
import sys
import types
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# Pin psycopg2 + its dynamically-populated errors module into sys.modules
# BEFORE any test takes a patch.dict(sys.modules, ...) snapshot. patch.dict
# restores by clear-then-update, so a module first imported inside the block
# is purged on exit -- and re-importing the purged psycopg2 C extension
# yields an errors module without its sqlstate classes, so the *second*
# endpoint test would fail with AttributeError: ReadOnlySqlTransaction.
# tests/test_cypher_read_only.py pins it the same way.
import psycopg2.errors  # noqa: E402,F401

import main  # noqa: E402
from cypher_rewrite import rewrite_order_by_aliases  # noqa: E402


def _norm(s: str) -> str:
    """Collapse whitespace so tests assert on tokens, not spacing."""
    return " ".join(s.split())


class TestOrderByAliasRewrite(unittest.TestCase):
    def assertRewrites(self, src: str, expected: str):
        self.assertEqual(_norm(rewrite_order_by_aliases(src)), _norm(expected))

    def assertUnchanged(self, src: str):
        self.assertEqual(rewrite_order_by_aliases(src), src)

    # ── the reported query ────────────────────────────────────────────
    def test_issue_209_aggregate_alias(self):
        self.assertRewrites(
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, "
            "count(*) AS n ORDER BY n DESC",
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, "
            "count(*) AS n ORDER BY (count(*)) DESC",
        )

    def test_non_aggregate_alias_also_rewritten(self):
        """The bug is general to aliases, not specific to aggregates."""
        self.assertRewrites(
            "UNWIND [3,1,2] AS x RETURN x AS k ORDER BY k",
            "UNWIND [3,1,2] AS x RETURN x AS k ORDER BY (x)",
        )

    def test_property_expression_alias(self):
        self.assertRewrites(
            "MATCH (n) RETURN n.name AS nm ORDER BY nm ASC",
            "MATCH (n) RETURN n.name AS nm ORDER BY (n.name) ASC",
        )

    # ── multi-item / trailing clauses ─────────────────────────────────
    def test_multi_item_order_by_mixes_alias_and_in_scope_var(self):
        self.assertRewrites(
            "UNWIND [3,1,2] AS x RETURN x AS k, count(*) AS n ORDER BY n DESC, x ASC",
            "UNWIND [3,1,2] AS x RETURN x AS k, count(*) AS n "
            "ORDER BY (count(*)) DESC, x ASC",
        )

    def test_skip_and_limit_are_preserved_and_untouched(self):
        self.assertRewrites(
            "MATCH (n) RETURN n.v AS v ORDER BY v DESC SKIP 5 LIMIT 10",
            "MATCH (n) RETURN n.v AS v ORDER BY (n.v) DESC SKIP 5 LIMIT 10",
        )

    def test_alias_inside_larger_expression(self):
        self.assertRewrites(
            "UNWIND [1,2] AS x RETURN count(*) AS n ORDER BY n * 2 DESC",
            "UNWIND [1,2] AS x RETURN count(*) AS n ORDER BY (count(*)) * 2 DESC",
        )

    def test_expression_with_nested_commas_and_parens(self):
        self.assertRewrites(
            "MATCH (n) RETURN coalesce(n.a, n.b) AS c ORDER BY c",
            "MATCH (n) RETURN coalesce(n.a, n.b) AS c ORDER BY (coalesce(n.a, n.b))",
        )

    def test_distinct_projection(self):
        self.assertRewrites(
            "UNWIND [3,1] AS x RETURN DISTINCT x AS k ORDER BY k",
            "UNWIND [3,1] AS x RETURN DISTINCT x AS k ORDER BY (x)",
        )

    def test_lowercase_keywords(self):
        self.assertRewrites(
            "match (n) return n.name as nm order by nm desc",
            "match (n) return n.name as nm order by (n.name) desc",
        )

    # ── WITH projections have the identical defect ────────────────────
    def test_with_clause_alias_rewritten(self):
        self.assertRewrites(
            "UNWIND [3,1,2] AS x WITH x AS k ORDER BY k RETURN k AS out",
            "UNWIND [3,1,2] AS x WITH x AS k ORDER BY (x) RETURN k AS out",
        )

    def test_with_and_return_each_rewritten_independently(self):
        self.assertRewrites(
            "MATCH (n) WITH n.a AS a ORDER BY a RETURN a AS b ORDER BY b",
            "MATCH (n) WITH n.a AS a ORDER BY (n.a) RETURN a AS b ORDER BY (a)",
        )

    # ── things that must NOT be rewritten ─────────────────────────────
    def test_no_order_by_is_unchanged(self):
        self.assertUnchanged("MATCH (n) RETURN n.name AS nm")

    def test_order_by_in_scope_variable_is_unchanged(self):
        self.assertUnchanged("UNWIND [3,1] AS x RETURN x AS k ORDER BY x")

    def test_unaliased_return_is_unchanged(self):
        self.assertUnchanged("MATCH (n) RETURN n ORDER BY n.name")

    def test_alias_name_inside_string_literal_is_not_substituted(self):
        self.assertUnchanged("MATCH (n) RETURN n.v AS v ORDER BY n.label = 'v'")

    def test_alias_name_as_property_key_is_not_substituted(self):
        """``x.n`` is a property lookup, not a reference to alias ``n``."""
        self.assertUnchanged("MATCH (x) RETURN count(*) AS n ORDER BY x.n")

    def test_alias_name_used_as_function_name_is_not_substituted(self):
        self.assertUnchanged("MATCH (x) RETURN x.a AS size ORDER BY size(x.list)")

    def test_asc_desc_keywords_are_not_treated_as_identifiers(self):
        self.assertUnchanged("MATCH (n) RETURN n.a AS desc ORDER BY n.a DESC")

    # ── alias shadowing a bound variable: must NOT be rewritten ───────
    #
    # When the alias name is also a variable bound before the projection,
    # AGE resolves ORDER BY to the BOUND VARIABLE and the query already
    # works. Rewriting would silently change the sort key. Measured on
    # production AGE 2026-09-10 with three distinguishable orderings:
    #
    #   UNWIND [[2,20],[1,30],[3,10]] AS p WITH p[0] AS a, p[1] AS b ...
    #     no ORDER BY          -> 20,30,10   (natural)
    #     ORDER BY a           -> 30,20,10   (by bound a)
    #     ORDER BY b           -> 10,20,30   (by b)
    #     RETURN b AS a ORDER BY a -> 30,20,10   <- binds to the VARIABLE
    #     RETURN b AS a ORDER BY (b) -> 10,20,30 <- what a rewrite would do
    #
    # So the rewrite is skipped for any alias whose name is already bound.

    def test_alias_shadowing_bound_match_variable_is_not_rewritten(self):
        self.assertUnchanged("MATCH (a)-->(b) RETURN b AS a ORDER BY a")

    def test_alias_shadowing_bound_unwind_variable_is_not_rewritten(self):
        self.assertUnchanged(
            "UNWIND [[2,20],[1,30],[3,10]] AS p WITH p[0] AS a, p[1] AS b "
            "RETURN b AS a ORDER BY a"
        )

    def test_alias_shadowing_own_node_variable_is_not_rewritten(self):
        self.assertUnchanged("MATCH (n) RETURN n.name AS n ORDER BY n")

    def test_only_the_shadowed_alias_is_skipped(self):
        """A shadowed alias must not disable the rewrite for its siblings."""
        self.assertRewrites(
            "MATCH (n) RETURN n.a AS n, count(*) AS c ORDER BY n, c DESC",
            "MATCH (n) RETURN n.a AS n, count(*) AS c ORDER BY n, (count(*)) DESC",
        )

    # ── near-misses that must NOT be mistaken for a bound variable ────

    def test_property_key_matching_alias_does_not_block_rewrite(self):
        """``x.n`` before the projection is a property key, not a binding."""
        self.assertRewrites(
            "MATCH (x) WHERE x.n > 1 RETURN x.n AS n ORDER BY n",
            "MATCH (x) WHERE x.n > 1 RETURN x.n AS n ORDER BY (x.n)",
        )

    def test_label_matching_alias_does_not_block_rewrite(self):
        """A label after ':' is not a binding."""
        self.assertRewrites(
            "MATCH (x:total) RETURN count(*) AS total ORDER BY total DESC",
            "MATCH (x:total) RETURN count(*) AS total ORDER BY (count(*)) DESC",
        )

    def test_relationship_type_matching_alias_does_not_block_rewrite(self):
        self.assertRewrites(
            "MATCH ()-[r:n]->() RETURN r.x AS n ORDER BY n",
            "MATCH ()-[r:n]->() RETURN r.x AS n ORDER BY (r.x)",
        )

    def test_map_literal_key_matching_alias_does_not_block_rewrite(self):
        """``{n: 3}`` is a map key, not a binding — found by probing the
        guard against live AGE, where this shape was skipped."""
        self.assertRewrites(
            "UNWIND [{n:3},{n:1}] AS x RETURN x.n AS n ORDER BY n DESC",
            "UNWIND [{n:3},{n:1}] AS x RETURN x.n AS n ORDER BY (x.n) DESC",
        )

    def test_pattern_variable_before_a_label_still_counts_as_bound(self):
        """The mirror of the above: ``(n:Label)`` looks identical to a map
        key but ``n`` IS bound, so the rewrite must still be skipped."""
        self.assertUnchanged("MATCH (n:Person) RETURN n.name AS n ORDER BY n")

    def test_mismatched_bracket_types_are_rejected(self):
        src = "MATCH (n] RETURN n.a AS a ORDER BY a"
        self.assertEqual(rewrite_order_by_aliases(src), src)

    def test_function_name_matching_alias_does_not_block_rewrite(self):
        """``size(...)`` before the projection is a call, not a binding."""
        self.assertRewrites(
            "MATCH (x) WHERE size(x.l) > 1 RETURN x.a AS size ORDER BY size",
            "MATCH (x) WHERE size(x.l) > 1 RETURN x.a AS size ORDER BY (x.a)",
        )

    def test_issue_209_query_is_still_rewritten_despite_the_guard(self):
        """The guard must not regress the case the issue is actually about."""
        self.assertRewrites(
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, "
            "count(*) AS n ORDER BY n DESC",
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, "
            "count(*) AS n ORDER BY (count(*)) DESC",
        )

    # ── robustness: never raise, never mangle ─────────────────────────
    def test_unbalanced_input_returns_original(self):
        src = "MATCH (n RETURN n.a AS a ORDER BY a"
        self.assertEqual(rewrite_order_by_aliases(src), src)

    def test_empty_and_trivial_inputs(self):
        for src in ("", "   ", "RETURN 1", "ORDER BY x"):
            self.assertEqual(rewrite_order_by_aliases(src), src)

    def test_non_string_input_returned_as_is(self):
        self.assertIsNone(rewrite_order_by_aliases(None))


class _CapturingKG:
    """Stub KnowledgeGraphAGE that records the Cypher it is handed."""

    seen: ClassVar[list] = []

    def __init__(self, dsn=None):
        self._conn = MagicMock()
        self._conn.cursor.return_value.__enter__.return_value = MagicMock()
        self._conn.cursor.return_value.__exit__.return_value = False

    def _run_cypher(self, cypher, params=None, fetch=False):
        type(self).seen.append(cypher)
        return [("x", 1)]

    def _extract_return_aliases(self, cypher):
        return ["rt", "n"]

    @staticmethod
    def _unwrap_agtype(val):
        return val

    def close(self):
        pass


class TestCypherEndpointAppliesRewrite(unittest.TestCase):
    """The /cypher route must hand AGE the rewritten source (#209)."""

    def _call(self, cypher: str):
        _CapturingKG.seen = []
        fake_mod = types.ModuleType("mempalace.knowledge_graph_age")
        fake_mod.KnowledgeGraphAGE = _CapturingKG
        mempalace_pkg = types.ModuleType("mempalace")
        mempalace_pkg.knowledge_graph_age = fake_mod
        fake_mp = types.SimpleNamespace(
            _config=types.SimpleNamespace(
                backend="postgres", postgres_dsn="postgresql://fake/db"
            )
        )
        req = MagicMock()

        async def _json():
            return {"cypher": cypher}

        req.json = _json

        with patch.dict(
            sys.modules,
            {"mempalace": mempalace_pkg, "mempalace.knowledge_graph_age": fake_mod},
            clear=False,
        ), patch.object(main, "_mp", fake_mp), patch.dict(os.environ, {}, clear=True):
            result = asyncio.run(main.cypher_query(req, x_api_key=None))
        return result, _CapturingKG.seen

    def test_order_by_alias_is_resolved_before_reaching_age(self):
        result, seen = self._call(
            "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, "
            "count(*) AS n ORDER BY n DESC"
        )
        self.assertEqual(len(seen), 1)
        self.assertIn("ORDER BY (count(*)) DESC", seen[0])
        self.assertNotIn("ORDER BY n DESC", seen[0])
        # The response envelope is unaffected by the rewrite.
        self.assertEqual(result["rows"], [{"rt": "x", "n": 1}])

    def test_query_without_order_by_reaches_age_verbatim(self):
        src = "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"
        _, seen = self._call(src)
        self.assertEqual(seen, [src])


if __name__ == "__main__":
    unittest.main()
