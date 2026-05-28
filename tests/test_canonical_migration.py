"""Tests for the canonical migration plan + apply guards (#72a).

`build_plan` is pure given a mapper — tested with a deterministic fake mapper
(no model load). `_apply`'s guards (DSN, backup ack) are tested to confirm the
migration REFUSES to mutate without both. The execution path itself is exercised
with a mocked psycopg connection so we verify the SQL string and COPY contents
without needing a real AGE database.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_canonical_migration.py -q
"""
import argparse
import importlib.util
import os
import unittest
from unittest.mock import MagicMock, patch

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

    def test_apply_with_empty_remaps_returns_quietly(self):
        # Both gates satisfied + nothing to do → no connection attempt, no SystemExit.
        # (Real psycopg.connect would fail on the fake DSN; this confirms we don't reach it.)
        with patch.object(mig, "_apply", wraps=mig._apply):
            mig._apply({"remaps": [], "edges_would_change": 0,
                        "edges_unchanged": 0, "dropped_edges": 0},
                       self._args(dsn="postgresql://x", i_have_a_backup=True))

    def test_apply_executes_set_based_update_via_psycopg(self):
        # Guarded apply with rules → COPY + single UPDATE on the AGE backing table.
        # We mock psycopg entirely; the test asserts the SQL shape and the COPY rows.
        plan = {
            "remaps": [{"raw": "is", "canonical": "is_a", "edges": 100},
                       {"raw": "has", "canonical": "contains", "edges": 50}],
            "edges_would_change": 150,
            "edges_unchanged": 5,
            "dropped_edges": 7,
        }

        fake_cur = MagicMock()
        fake_cur.rowcount = 150
        fake_cur.fetchone.return_value = (12,)  # remaining without raw_relation_type
        # cur.copy(...) is a context manager that yields a writer with write_row.
        fake_copy_writer = MagicMock()
        fake_cur.copy.return_value.__enter__.return_value = fake_copy_writer

        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cur

        fake_psycopg = MagicMock()
        fake_psycopg.connect.return_value.__enter__.return_value = fake_conn

        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            mig._apply(plan, self._args(dsn="postgresql://x", i_have_a_backup=True))

        # Both rules COPY'd in order
        rows = [c.args for c in fake_copy_writer.write_row.call_args_list]
        self.assertEqual(rows, [(("is", "is_a"),), (("has", "contains"),)])

        # An UPDATE statement actually ran with the documented shape
        sql_statements = [str(c.args[0]) for c in fake_cur.execute.call_args_list]
        update_sql = next((s for s in sql_statements if "UPDATE" in s and "RELATION" in s), "")
        self.assertIn('mempalace_kg."RELATION"', update_sql)
        self.assertIn("raw_relation_type", update_sql)
        self.assertIn("ag_catalog.agtype", update_sql)  # fully-qualified cast
        self.assertIn("predicate_mapping", update_sql)  # joins the TEMP mapping
        self.assertIn("?", update_sql)  # the IS NULL-equivalent jsonb membership check

        # Commit happened (set-based UPDATE in a single transaction)
        fake_conn.commit.assert_called()


class TestLoadVocabErrors(unittest.TestCase):
    def test_missing_file(self):
        ns = argparse.Namespace(freq_file="/no/such.json")
        with self.assertRaises(SystemExit) as cm:
            mig._load_vocab(ns)
        self.assertIn("cannot read", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
