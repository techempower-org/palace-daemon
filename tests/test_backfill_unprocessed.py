"""Unit tests for the `unprocessed_drawers` breakdown in
`/backfill-age/status`.

Pinpoints the bucket logic that splits drawers missing from the AGE
checkpoint into reason codes. The query itself runs against a real
postgres connection in production; tests mock the cursor so they need
no DB, no palace, and no fixtures.

Run::

    cd /path/to/palace-daemon
    python -m unittest tests.test_backfill_unprocessed -v
"""
import os
import sys
import unittest
from unittest.mock import MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402


def _make_conn_returning(row):
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn.cursor.return_value.__enter__.return_value = cur
    return conn


class TestBackfillUnprocessedBreakdown(unittest.TestCase):
    def test_only_nonzero_codes_returned(self):
        # total=10, no_filed_at=0, pre_run_unmarked=0, during_run=10,
        # after_run=0 — only `added_during_run` should appear.
        conn = _make_conn_returning((10, 0, 0, 10, 0))
        total, codes = main._backfill_unprocessed_breakdown(conn)
        self.assertEqual(total, 10)
        self.assertEqual(codes, {"added_during_run": 10})

    def test_all_buckets_populated(self):
        # total=10, no_filed_at=1, pre_run_unmarked=2, during_run=4, after_run=3
        conn = _make_conn_returning((10, 1, 2, 4, 3))
        total, codes = main._backfill_unprocessed_breakdown(conn)
        self.assertEqual(total, 10)
        self.assertEqual(
            codes,
            {
                "added_during_run": 4,
                "added_after_run": 3,
                "pre_run_unmarked": 2,
                "no_filed_at": 1,
            },
        )

    def test_zero_gap_returns_empty_codes(self):
        # No unprocessed drawers — codes dict should be empty, not zeroed.
        conn = _make_conn_returning((0, 0, 0, 0, 0))
        total, codes = main._backfill_unprocessed_breakdown(conn)
        self.assertEqual(total, 0)
        self.assertEqual(codes, {})

    def test_statement_timeout_applied(self):
        # The query must set a local statement_timeout so a slow scan
        # doesn't hang the status endpoint. Check the SQL string passed.
        conn = _make_conn_returning((0, 0, 0, 0, 0))
        main._backfill_unprocessed_breakdown(conn)
        cur = conn.cursor.return_value.__enter__.return_value
        sql_executed = cur.execute.call_args[0][0]
        self.assertIn("SET LOCAL statement_timeout", sql_executed)
        self.assertIn("LEFT JOIN mempalace_kg_backfill_state", sql_executed)


if __name__ == "__main__":
    unittest.main()
