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

    def test_drops_list_captures_dropped_raws(self):
        # The dropped (map_predicate → None) raws are captured for the DELETE
        # pass — exactly the edges counted in dropped_edges, no separate set.
        self.assertEqual(self.plan["drops"], [{"raw": "appendchild", "edges": 7}])
        self.assertEqual(self.plan["top_drops"], [{"raw": "appendchild", "edges": 7}])
        # invariant: drop edge total == reported dropped_edges (no drift)
        self.assertEqual(sum(d["edges"] for d in self.plan["drops"]),
                         self.plan["dropped_edges"])


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


class TestRemapExisting(unittest.TestCase):
    """#45: --remap-existing re-evaluates already-migrated edges.

    Two behavioral switches: the frequency read uses the ORIGINAL predicate
    (coalesce(raw_relation_type, relation_type)), and the apply UPDATE keys on
    that original + drops the first-migration guard. Default OFF must be
    byte-for-byte the original first-migration SQL.
    """

    def _args(self, **kw):
        base = dict(apply=True, dsn="postgresql://x", i_have_a_backup=True,
                    drop_code_tokens=False, remap_existing=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def _run_apply(self, args):
        """Run _apply with a mocked psycopg; return the executed SQL strings."""
        fake_cur = MagicMock()
        fake_cur.rowcount = 42
        fake_cur.fetchone.return_value = (0,)
        fake_cur.copy.return_value.__enter__.return_value = MagicMock()
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cur
        fake_psycopg = MagicMock()
        fake_psycopg.connect.return_value.__enter__.return_value = fake_conn
        plan = {
            "remaps": [{"raw": "is", "canonical": "is_a", "edges": 100}],
            "edges_would_change": 100, "edges_unchanged": 0, "dropped_edges": 0,
        }
        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            mig._apply(plan, args)
        return [str(c.args[0]) for c in fake_cur.execute.call_args_list]

    def test_freq_query_reads_original_predicate_when_remap_existing(self):
        # _FREQ_CYPHER_REMAP coalesces raw_relation_type so the mapper sees the
        # original, not the prior canonical.
        self.assertIn("coalesce(r.raw_relation_type, r.relation_type)",
                      mig._FREQ_CYPHER_REMAP)
        # default cypher unchanged (no coalesce)
        self.assertNotIn("coalesce", mig._FREQ_CYPHER)

    def test_fetch_vocab_uses_remap_cypher(self):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data.decode()
            cm = MagicMock()
            cm.__enter__.return_value.read.return_value = b'{"rows": []}'
            return cm

        env = {"PALACE_API_KEY": "k", "PALACE_DAEMON_URL": "http://d"}
        with patch.dict(os.environ, env), \
             patch("urllib.request.urlopen", side_effect=fake_urlopen):
            mig._fetch_vocab_readonly(remap_existing=True)
        self.assertIn("coalesce(r.raw_relation_type, r.relation_type)",
                      captured["body"])

    def test_remap_existing_apply_drops_guard_and_keys_on_original(self):
        sqls = self._run_apply(self._args(remap_existing=True))
        update = next((s for s in sqls if "UPDATE" in s and "RELATION" in s), "")
        # keys on the coalesced ORIGINAL predicate
        self.assertIn("COALESCE", update)
        self.assertIn("raw_relation_type", update)
        # only touches rows whose canonical actually changes
        self.assertIn("IS DISTINCT FROM", update)
        # the first-migration guard is ABSENT in remap mode
        self.assertNotIn("NOT ((e.properties::text::jsonb) ? 'raw_relation_type')",
                         update)

    def test_default_apply_keeps_first_migration_guard(self):
        # remap_existing=False (default) → original SQL with the guard intact.
        sqls = self._run_apply(self._args(remap_existing=False))
        update = next((s for s in sqls if "UPDATE" in s and "RELATION" in s), "")
        self.assertIn("NOT ((e.properties::text::jsonb) ? 'raw_relation_type')",
                      update)
        self.assertNotIn("IS DISTINCT FROM", update)

    def test_missing_remap_existing_attr_defaults_false(self):
        # _apply must tolerate a Namespace without the attr (back-compat with
        # callers built before #45) via getattr(..., False).
        ns = argparse.Namespace(apply=True, dsn="postgresql://x",
                                i_have_a_backup=True, drop_code_tokens=False)
        sqls = self._run_apply(ns)
        update = next((s for s in sqls if "UPDATE" in s and "RELATION" in s), "")
        self.assertIn("NOT ((e.properties::text::jsonb) ? 'raw_relation_type')",
                      update)


class TestDropCodeTokens(unittest.TestCase):
    """#72b: --drop-code-tokens DELETEs the blocklisted junk-predicate edges.

    Targeted set-based DELETE (TEMP drop_predicate + COPY + one DELETE keyed on
    the coalesced ORIGINAL predicate), NOT a remap-plan piggyback. Reachable
    standalone (no remaps) and idempotent by construction. psycopg is mocked so
    we assert SQL shape + COPY rows without a real AGE database.
    """

    def _args(self, **kw):
        base = dict(apply=True, dsn="postgresql://x", i_have_a_backup=True,
                    drop_code_tokens=True, remap_existing=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def _mock_psycopg(self, deleted_rows=999):
        fake_cur = MagicMock()
        fake_cur.rowcount = deleted_rows
        fake_cur.fetchone.return_value = (0,)
        fake_copy_writer = MagicMock()
        fake_cur.copy.return_value.__enter__.return_value = fake_copy_writer
        fake_conn = MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cur
        fake_psycopg = MagicMock()
        fake_psycopg.connect.return_value.__enter__.return_value = fake_conn
        return fake_psycopg, fake_conn, fake_cur, fake_copy_writer

    def test_delete_unit_sql_shape_and_copy_rows(self):
        # Direct unit test of _drop_code_tokens: TEMP table, COPY, one DELETE.
        plan = {"drops": [{"raw": "cd", "edges": 2543},
                          {"raw": "ls", "edges": 2020},
                          {"raw": "for", "edges": 1267}],
                "dropped_edges": 5830}
        fake_psycopg, fake_conn, fake_cur, fake_copy_writer = self._mock_psycopg(5830)

        mig._drop_code_tokens(plan, "postgresql://x", fake_psycopg)

        # all blocklisted raws COPY'd into the temp table, in order
        rows = [c.args for c in fake_copy_writer.write_row.call_args_list]
        self.assertEqual(rows, [(("cd",),), (("ls",),), (("for",),)])

        sqls = [str(c.args[0]) for c in fake_cur.execute.call_args_list]
        # a TEMP drop_predicate table was created
        self.assertTrue(any("drop_predicate" in s and "TEMP TABLE" in s
                            for s in sqls))
        delete_sql = next((s for s in sqls if "DELETE" in s and "RELATION" in s), "")
        self.assertIn('mempalace_kg."RELATION"', delete_sql)
        self.assertIn("drop_predicate", delete_sql)          # joins the temp set
        self.assertIn("COALESCE", delete_sql)                # keys on ORIGINAL predicate
        self.assertIn("raw_relation_type", delete_sql)
        self.assertIn("relation_type", delete_sql)
        # it is a DELETE, not an UPDATE/remap
        self.assertNotIn("UPDATE", delete_sql)
        fake_conn.commit.assert_called()

    def test_delete_noop_when_no_drops(self):
        # Empty drop set → returns quietly, never opens a connection.
        fake_psycopg, _, _, _ = self._mock_psycopg()
        mig._drop_code_tokens({"drops": [], "dropped_edges": 0},
                              "postgresql://x", fake_psycopg)
        fake_psycopg.connect.assert_not_called()

    def test_apply_runs_drop_pass_after_remap(self):
        # --apply with both remaps AND --drop-code-tokens → UPDATE then DELETE.
        plan = {
            "remaps": [{"raw": "is", "canonical": "is_a", "edges": 100}],
            "drops": [{"raw": "cd", "edges": 50}],
            "edges_would_change": 100, "edges_unchanged": 0, "dropped_edges": 50,
        }
        fake_psycopg, _, fake_cur, _ = self._mock_psycopg(50)
        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            mig._apply(plan, self._args(drop_code_tokens=True))
        sqls = [str(c.args[0]) for c in fake_cur.execute.call_args_list]
        self.assertTrue(any("UPDATE" in s and "RELATION" in s for s in sqls))
        self.assertTrue(any("DELETE" in s and "RELATION" in s for s in sqls))

    def test_apply_runs_drop_pass_standalone_when_no_remaps(self):
        # Graph already canonical (no remaps) but code tokens still present:
        # --drop-code-tokens must STILL reach the DELETE (not short-circuit).
        plan = {
            "remaps": [],
            "drops": [{"raw": "ls", "edges": 30}],
            "edges_would_change": 0, "edges_unchanged": 0, "dropped_edges": 30,
        }
        fake_psycopg, _, fake_cur, _ = self._mock_psycopg(30)
        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            mig._apply(plan, self._args(drop_code_tokens=True))
        sqls = [str(c.args[0]) for c in fake_cur.execute.call_args_list]
        self.assertTrue(any("DELETE" in s and "RELATION" in s for s in sqls))
        # no UPDATE ran (nothing to remap)
        self.assertFalse(any("UPDATE" in s and "RELATION" in s for s in sqls))

    def test_apply_no_drop_pass_without_flag(self):
        # No --drop-code-tokens → DELETE never runs, even with drops in the plan.
        plan = {
            "remaps": [{"raw": "is", "canonical": "is_a", "edges": 100}],
            "drops": [{"raw": "cd", "edges": 50}],
            "edges_would_change": 100, "edges_unchanged": 0, "dropped_edges": 50,
        }
        fake_psycopg, _, fake_cur, _ = self._mock_psycopg()
        with patch.dict("sys.modules", {"psycopg": fake_psycopg}):
            mig._apply(plan, self._args(drop_code_tokens=False))
        sqls = [str(c.args[0]) for c in fake_cur.execute.call_args_list]
        self.assertFalse(any("DELETE" in s and "RELATION" in s for s in sqls))


class TestDropCodeTokensIntegration(unittest.TestCase):
    """End-to-end-ish: build_plan with the REAL CanonicalMapper feeds the drop
    set, so the DELETE targets exactly the blocklisted predicates the mapper
    drops. No model load — uses the lexical mapper.
    """

    def test_real_mapper_drops_match_blocklists(self):
        from mempalace.kg_predicate_norm import (
            SHELL_COMMAND_BLOCKLIST,
            STOPWORD_BLOCKLIST,
        )

        vocab = [
            {"rt": "cd", "n": 2543},        # shell command → drop
            {"rt": "ls", "n": 2020},        # shell command → drop
            {"rt": "for", "n": 1267},       # stopword → drop
            {"rt": "is", "n": 5000},        # real relation → is_a (not dropped)
            {"rt": "uses", "n": 800},       # canonical (not dropped)
        ]
        mapper = mig.CanonicalMapper(use_embeddings=False)  # lexical, no model
        plan = mig.build_plan(vocab, mapper)

        dropped_raws = {d["raw"] for d in plan["drops"]}
        self.assertIn("cd", dropped_raws)
        self.assertIn("ls", dropped_raws)
        self.assertIn("for", dropped_raws)
        # real relations are NOT in the drop set
        self.assertNotIn("is", dropped_raws)
        self.assertNotIn("uses", dropped_raws)
        # every dropped raw really is a blocklisted/heuristic-junk token
        for raw in dropped_raws:
            self.assertTrue(
                raw in SHELL_COMMAND_BLOCKLIST or raw in STOPWORD_BLOCKLIST,
                f"{raw!r} dropped but not in a blocklist",
            )


if __name__ == "__main__":
    unittest.main()
