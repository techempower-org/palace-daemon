"""Tests for KG predicate normalization (kg_predicate_norm.py, issue #50).

Covers the three contamination classes from the issue:
  1. code tokens treated as predicates  → dropped (None)
  2. near-synonyms not collapsed         → canonicalized
  3. grammatical fragments / negation    → punctuation stripped, polarity
                                            denormalized to not_<base>

Plus folding edge cases and idempotency.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_kg_predicate_norm.py -q
"""
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kg_predicate_norm import (  # noqa: E402
    CODE_TOKEN_BLOCKLIST,
    SYNONYM_MAP,
    normalize_predicate,
)


class TestClass1CodeTokens(unittest.TestCase):
    """Code-token predicates from the issue must be dropped (None)."""

    def test_issue_examples_dropped(self):
        for tok in ("appendchild", "createelement", "executemany",
                    "setattribute", "getelementbyid"):
            self.assertIsNone(normalize_predicate(tok), tok)

    def test_camelcase_form_dropped(self):
        # The extractor may emit either case; both fold to the blocklist form.
        for tok in ("appendChild", "createElement", "getElementById",
                    "setAttribute", "executeMany"):
            self.assertIsNone(normalize_predicate(tok), tok)

    def test_digit_identifier_heuristic_dropped(self):
        # single lowercase token with embedded digit → code noise
        for tok in ("utf8decode", "sha256hash", "base64encode"):
            self.assertIsNone(normalize_predicate(tok), tok)

    def test_legitimate_verb_with_no_digit_survives(self):
        # heuristic must not eat real single-word verbs
        for verb in ("uses", "owns", "contains", "created"):
            self.assertIsNotNone(normalize_predicate(verb), verb)

    def test_snake_phrase_survives_heuristic(self):
        # an underscore means it is a phrase, not a code identifier
        self.assertEqual(normalize_predicate("works_on"), "works_on")

    def test_negated_code_token_drops_whole_predicate(self):
        # "doesn't appendChild" should still drop, not become not_append_child
        self.assertIsNone(normalize_predicate("doesnt_appendchild"))


class TestClass2Synonyms(unittest.TestCase):
    """Near-synonyms collapse to a single canonical relation type."""

    def test_identity_family_collapses_to_is_a(self):
        for raw in ("is", "is_a", "is_an", "was_a", "is_an_instance_of",
                    "is_a_kind_of", "is_a_type_of", "instance_of"):
            self.assertEqual(normalize_predicate(raw), "is_a", raw)

    def test_part_of_family(self):
        for raw in ("is_part_of", "is_a_part_of", "belongs_to", "member_of"):
            self.assertEqual(normalize_predicate(raw), "part_of", raw)

    def test_reference_family(self):
        for raw in ("is_a_reference", "refers_to", "reference",
                    "is_reference_to"):
            self.assertEqual(normalize_predicate(raw), "references", raw)

    def test_distinct_relations_not_merged(self):
        # part_of must NOT collapse into is_a — they are different edges
        self.assertNotEqual(
            normalize_predicate("part_of"), normalize_predicate("is_a")
        )
        self.assertEqual(normalize_predicate("part_of"), "part_of")
        self.assertEqual(normalize_predicate("created_by"), "created_by")

    def test_case_and_spacing_variation_collapse(self):
        # surface variants fold then hit the synonym map
        self.assertEqual(normalize_predicate("Is A"), "is_a")
        self.assertEqual(normalize_predicate("is-a"), "is_a")
        self.assertEqual(normalize_predicate("IS_AN_INSTANCE_OF"), "is_a")


class TestClass3Negation(unittest.TestCase):
    """Negation/punctuation fragments → stripped + polarity denormalized."""

    def test_issue_apostrophe_examples(self):
        # don't_adapt → not_adapt ; aren't_merged → not_merged
        self.assertEqual(normalize_predicate("don't_adapt"), "not_adapt")
        self.assertEqual(normalize_predicate("aren't_merged"), "not_merged")
        self.assertEqual(normalize_predicate("'doesn't_appear'"), "not_appear")

    def test_apostrophe_stripped_from_endpoints(self):
        # leading/trailing quotes removed
        self.assertEqual(normalize_predicate("'uses'"), "uses")
        self.assertEqual(normalize_predicate('"contains"'), "contains")

    def test_negation_prefix_variants_unify(self):
        # both contraction and expanded form land on the same not_<base>
        self.assertEqual(normalize_predicate("doesnt_appear"), "not_appear")
        self.assertEqual(normalize_predicate("does_not_appear"), "not_appear")

    def test_negation_applied_after_synonym_collapse(self):
        # negation peels first, then the *base* is canonicalized, then we
        # re-prefix not_. "is not a part of" → base "a_part_of" → part_of
        # → not_part_of.
        self.assertEqual(normalize_predicate("is_not_a_part_of"), "not_part_of")
        # "isn't a" strips the "isnt_" prefix leaving base "a" (not a known
        # synonym), so the result is not_a — consistent peel-then-canonicalize.
        self.assertEqual(normalize_predicate("isnt_a"), "not_a")

    def test_bare_negation_token_drops(self):
        self.assertIsNone(normalize_predicate("not_"))
        self.assertIsNone(normalize_predicate("doesnt_"))

    def test_positive_predicate_unaffected(self):
        self.assertEqual(normalize_predicate("adapts"), "adapts")


class TestFoldingEdgeCases(unittest.TestCase):
    def test_empty_and_whitespace(self):
        self.assertIsNone(normalize_predicate(""))
        self.assertIsNone(normalize_predicate("   "))

    def test_punctuation_only(self):
        self.assertIsNone(normalize_predicate("'''"))
        self.assertIsNone(normalize_predicate("___"))

    def test_non_string_input(self):
        self.assertIsNone(normalize_predicate(None))  # type: ignore[arg-type]
        self.assertIsNone(normalize_predicate(123))  # type: ignore[arg-type]

    def test_multi_underscore_collapsed(self):
        self.assertEqual(normalize_predicate("works___on"), "works_on")

    def test_leading_trailing_underscores_trimmed(self):
        self.assertEqual(normalize_predicate("_uses_"), "uses")


class TestIdempotency(unittest.TestCase):
    """normalize_predicate(normalize_predicate(x)) == normalize_predicate(x)."""

    def test_idempotent_over_samples(self):
        samples = [
            "is", "is_a", "appendChild", "don't_adapt", "is_an_instance_of",
            "uses", "part_of", "doesnt_appear", "references", "works on",
        ]
        for raw in samples:
            once = normalize_predicate(raw)
            if once is None:
                continue
            twice = normalize_predicate(once)
            self.assertEqual(once, twice, f"not idempotent: {raw!r}")

    def test_canonical_values_are_fixed_points(self):
        # every canonical target must normalize to itself
        for canonical in set(SYNONYM_MAP.values()):
            self.assertEqual(normalize_predicate(canonical), canonical)

    def test_blocklist_entries_all_drop(self):
        for tok in CODE_TOKEN_BLOCKLIST:
            self.assertIsNone(normalize_predicate(tok), tok)


if __name__ == "__main__":
    unittest.main()
