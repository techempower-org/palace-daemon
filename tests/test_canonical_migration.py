"""Tests for the canonical migration plan + apply guards (#72a).

`build_plan` is pure given a mapper — tested with a deterministic fake mapper
(no model load). The `_apply` guards are tested to confirm the migration
REFUSES to mutate without a DSN and an explicit backup acknowledgement.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_canonical_migration.py -q
"""
import argparse
import importlib.util
import os
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SCRIPT = os.path.join(_ROOT, "scripts", "canonical_migration.py")

_spec = importlib.util.spec_from_file_location("canonical_migration", _SCRIPT)
mig = importlib.util.module_from_spec(_spec)
assert _spec and _spec.loader
_spec.loader.exec_module(mig)


class _FakeMapper:
    def __init__(self, table):
        self.table = table

    def map_predicate(self, raw):
        return self.table.get(raw, ("other", 0.0))


class TestBuildPlan(unittest.TestCase):
    def setUp(self):
        # raw → canonical
        self.mapper = _FakeMapper({
            "is": ("is_a", 0.9),
            "are": ("is_a", 0.8),
            "is_a": ("is_a", 1.0),       # already canonical (unchanged)
            "has": ("contains", 0.7),
            "appendchild": (None, 0.0),  # dropped code token
            "weird": ("other", 0.1),     # long tail
        })
        self.vocab = [
            {"rt": "is", "n": 100},
            {"rt": "are", "n": 20},
            {"rt": "is_a", "n": 5},
            {"rt": "has", "n": 50},
            {"rt": "appendchild", "n": 7},
            {"rt": "weird", "n": 3},
        ]
        self.plan = mig.build_plan(self.vocab, self.mapper)

    def test_total_edges(self):
        self.assertEqual(self.plan["total_edges"], 185)

    def test_after_distinct_collapses(self):
        # canonicals used: is_a, contains, other  → 3
        self.assertEqual(self.plan["after_distinct"], 3)

    def test_edges_would_change(self):
        # changed: is(100)+are(20)+has(50)+weird(3) = 173 ; is_a unchanged(5);
        # appendchild dropped(7) not counted as change
        self.assertEqual(self.plan["edges_would_change"], 173)

    def test_unchanged_edges(self):
        self.assertEqual(self.plan["edges_unchanged"], 5)

    def test_dropped(self):
        self.assertEqual(self.plan["dropped_edges"], 7)
        self.assertEqual(self.plan["dropped_distinct"], 1)

    def test_other_bucket(self):
        self.assertEqual(self.plan["other_edges"], 3)
        self.assertEqual(self.plan["other_distinct"], 1)

    def test_top_remap_is_largest(self):
        self.assertEqual(self.plan["top_remaps"][0]["raw"], "is")
        self.assertEqual(self.plan["top_remaps"][0]["canonical"], "is_a")
        self.assertEqual(self.plan["top_remaps"][0]["edges"], 100)

    def test_distinct_remapped_count(self):
        # is, are, has, weird → 4 remap rules (is_a unchanged, appendchild dropped)
        self.assertEqual(self.plan["distinct_remapped"], 4)


class TestApplyGuards(unittest.TestCase):
    def _args(self, **kw):
        base = dict(apply=True, dsn=None, i_have_a_backup=False,
                    drop_code_tokens=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_apply_without_dsn_refuses(self):
        saved = os.environ.pop("MEMPALACE_POSTGRES_DSN", None)
        try:
            with self.assertRaises(SystemExit) as cm:
                mig._apply({"remaps": []}, self._args(dsn=None))
            self.assertIn("postgres", str(cm.exception).lower())
        finally:
            if saved is not None:
                os.environ["MEMPALACE_POSTGRES_DSN"] = saved

    def test_apply_without_backup_ack_refuses(self):
        with self.assertRaises(SystemExit) as cm:
            mig._apply({"remaps": []},
                       self._args(dsn="postgresql://x", i_have_a_backup=False))
        self.assertIn("backup", str(cm.exception).lower())

    def test_apply_with_backup_still_not_auto_executed(self):
        # even fully acknowledged, this PR's apply path is intentionally inert
        with self.assertRaises(SystemExit) as cm:
            mig._apply({"remaps": []},
                       self._args(dsn="postgresql://x", i_have_a_backup=True))
        self.assertIn("not auto-executed", str(cm.exception).lower())


class TestLoadVocabErrors(unittest.TestCase):
    def test_missing_file(self):
        ns = argparse.Namespace(freq_file="/no/such.json")
        with self.assertRaises(SystemExit) as cm:
            mig._load_vocab(ns)
        self.assertIn("cannot read", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
