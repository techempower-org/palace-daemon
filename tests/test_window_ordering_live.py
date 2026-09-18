"""The wall-clock ordering contract, pinned against a real postgres.

mempalace#500 ships **option B**: `/window` matches the contract already
documented in `mempalace/date_window.py` — *"any timezone offset on the
input is dropped… comparison is therefore wall-clock"* — so it changes no
cross-surface behaviour and makes the existing semantics fast and pageable.

Asserting that on SQL *text* would only restate the builder. These tests
run the real statement against a real database, because the property that
matters is an ordering, and an ordering is a thing rows do.

The pinning test the lead asked for: a `Z`-stamped row and a naive row
whose wall-clock fields are equal must sort **adjacently**. Under B they
do, because the `Z` is compared as its literal digits. When the write-side
convention is unified (mempalace#506) and `/window` moves to `instant`
ordering, that adjacency breaks — and this test is the thing that says so
out loud instead of the ordering quietly changing under a live palace.

Needs a scratch postgres. Set ``WINDOW_TEST_DSN`` to run; skipped
otherwise, so the suite stays green without a database (palace-daemon has
no CI, and the mempalace matrix installs without the postgres extra).

    WINDOW_TEST_DSN=postgresql://mempalace:...@172.19.0.2:5432/mempalace \\
      PYTHONPATH=. venv/bin/python -m pytest tests/test_window_ordering_live.py -q
"""
from __future__ import annotations

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import window_query as wq  # noqa: E402

DSN = os.environ.get("WINDOW_TEST_DSN")
_TABLE = "window_test_drawers"


def _psycopg():
    try:
        import psycopg

        return psycopg
    except ImportError:  # pragma: no cover - no postgres extra installed
        return None


@unittest.skipUnless(DSN and _psycopg(), "set WINDOW_TEST_DSN and install psycopg")
class TestWallClockOrderingLive(unittest.TestCase):
    """Rows, not SQL strings. The subject is what the database returns."""

    @classmethod
    def setUpClass(cls):
        psycopg = _psycopg()
        cls.conn = psycopg.connect(DSN)
        cls.conn.autocommit = True
        with cls.conn.cursor() as cur:
            # A stand-in with the production column shape (no created_at —
            # that is the finding this whole route is built around).
            cur.execute(f"DROP TABLE IF EXISTS {_TABLE}")
            cur.execute(
                f"""CREATE TABLE {_TABLE} (
                       id text PRIMARY KEY,
                       wing text NOT NULL DEFAULT '',
                       room text NOT NULL DEFAULT '',
                       document text NOT NULL DEFAULT '',
                       metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb)"""
            )

    @classmethod
    def tearDownClass(cls):
        with cls.conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {_TABLE}")
        cls.conn.close()

    def setUp(self):
        with self.conn.cursor() as cur:
            cur.execute(f"DELETE FROM {_TABLE}")

    def insert(self, drawer_id, filed_at, *, wing="w", room="", chunk=None, source=None):
        import json

        meta = {"wing": wing, "room": room}
        if filed_at is not None:
            meta["filed_at"] = filed_at
        if chunk is not None:
            meta["chunk_index"] = chunk
        if source is not None:
            meta["source_file"] = source
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {_TABLE} (id, wing, room, document, metadata) "
                "VALUES (%s, %s, %s, %s, %s::jsonb)",
                (drawer_id, wing, room, "doc", json.dumps(meta)),
            )

    def run_sql(self, sql, params):
        sql = sql.replace(wq.TABLE, _TABLE)
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            return [r[0] for r in cur.fetchall()]

    def window(self, **kw):
        kw.setdefault("wing", "w")
        return self.run_sql(*wq.build_window_sql(**kw))

    # -- the pinning test -------------------------------------------------

    def test_a_Z_row_and_a_naive_row_with_equal_wall_clock_sort_adjacently(self):
        """Pins option B. Breaks deliberately when #506's fix lands.

        Production holds 915,562 naive host-local values and 5,474 ending
        in Z. Under the wall-clock contract the Z is compared as its
        literal digits, so these two land next to each other. Under a
        normalised (instant) ordering they would be 7 hours apart — which
        is the correct chronology and a different contract.
        """
        self.insert("b_naive", "2026-07-07T18:21:32.000000")
        self.insert("a_zulu", "2026-07-07T18:21:32.000Z")
        self.insert("c_later", "2026-07-08T09:00:00.000000")
        self.insert("z_earlier", "2026-07-06T09:00:00.000000")

        order = self.window()

        self.assertEqual(order[0], "z_earlier")
        self.assertEqual(order[-1], "c_later")
        middle = order[1:3]
        self.assertEqual(
            set(middle),
            {"a_zulu", "b_naive"},
            "the Z row and the naive row with the same wall clock must be adjacent "
            f"under the wall-clock contract; got {order}",
        )

    def test_the_same_pair_is_NOT_adjacent_under_instant_ordering(self):
        """The control for the test above: prove it can tell the two apart.

        Without this, 'they sort adjacently' might hold for any ordering and
        the pinning test would assert nothing.
        """
        self.insert("b_naive", "2026-07-07T18:21:32.000000")
        self.insert("a_zulu", "2026-07-07T18:21:32.000Z")
        self.insert("mid", "2026-07-07T13:00:00.000000")

        order = self.window(ordering="instant", tz_name="America/Los_Angeles")

        self.assertEqual(
            order,
            ["a_zulu", "mid", "b_naive"],
            "normalised: 18:21Z is 11:21 local, so it precedes 13:00 local; "
            f"got {order}",
        )

    # -- the contract's boundary semantics --------------------------------

    def test_since_includes_its_boundary_and_before_excludes_its_own(self):
        self.insert("at_since", "2026-09-01T00:00:00.000000")
        self.insert("inside", "2026-09-02T00:00:00.000000")
        self.insert("at_before", "2026-09-03T00:00:00.000000")

        order = self.window(since="2026-09-01T00:00:00", before="2026-09-03T00:00:00")

        self.assertEqual(order, ["at_since", "inside"])

    def test_a_drawer_with_no_filed_at_is_excluded_under_a_bound(self):
        self.insert("timed", "2026-09-02T00:00:00.000000")
        self.insert("untimed", None)

        self.assertEqual(self.window(since="2026-09-01"), ["timed"])

    def test_a_drawer_with_no_filed_at_still_lists_with_no_bound(self):
        """Excluded by a bound, not erased from the palace."""
        self.insert("timed", "2026-09-02T00:00:00.000000")
        self.insert("untimed", None)

        self.assertEqual(set(self.window()), {"timed", "untimed"})

    # -- keyset pagination ------------------------------------------------

    def test_paging_with_the_cursor_visits_every_row_exactly_once(self):
        for i in range(25):
            self.insert(f"d{i:02d}", f"2026-09-02T10:00:{i:02d}.000000")

        seen, cursor = [], None
        for _ in range(10):
            page = self.window(limit=7, cursor=cursor)
            if not page:
                break
            seen.extend(page)
            last = page[-1]
            rows = self.run_sql(
                f"SELECT (d.metadata->>'filed_at') FROM {wq.TABLE} d WHERE d.id = %s", [last]
            )
            cursor = wq.encode_cursor(rows[0], last)

        self.assertEqual(len(seen), 25, f"paged {len(seen)} of 25")
        self.assertEqual(len(set(seen)), 25, "a row was returned twice")
        self.assertEqual(seen, sorted(seen), "pages were not in order")

    def test_rows_sharing_one_filed_at_to_the_microsecond_are_not_skipped(self):
        """The reason the cursor is a tuple comparison and not a scalar.

        Four drawers in the production sample shared one filed_at exactly;
        a `key > cursor_key` predicate drops every row on the boundary.
        """
        same = "2026-09-02T10:00:00.000000"
        for i in range(4):
            self.insert(f"tie{i}", same)
        self.insert("after", "2026-09-02T10:00:01.000000")

        first = self.window(limit=2)
        self.assertEqual(len(first), 2)
        cursor = wq.encode_cursor(same, first[-1])
        rest = self.window(cursor=cursor)

        self.assertEqual(
            sorted(first + rest),
            ["after", "tie0", "tie1", "tie2", "tie3"],
            "a tied timestamp must not swallow the rows after the page boundary",
        )

    # -- #502, by source file ---------------------------------------------

    def test_source_listing_is_in_numeric_chunk_order(self):
        for i in (0, 1, 2, 10, 11):
            self.insert(f"c{i}", f"2026-09-02T10:00:00.000000", chunk=str(i), source="/a/b/t.jsonl")

        order = self.run_sql(*wq.build_source_sql(source_file="t.jsonl"))

        self.assertEqual(
            order,
            ["c0", "c1", "c2", "c10", "c11"],
            "lexical ordering would put chunk 10 before chunk 2",
        )

    def test_source_listing_matches_on_the_basename_the_user_pastes(self):
        self.insert("x", "2026-09-02T10:00:00.000000", chunk="0", source="/long/path/t.jsonl")
        self.assertEqual(self.run_sql(*wq.build_source_sql(source_file="t.jsonl")), ["x"])
        self.assertEqual(
            self.run_sql(*wq.build_source_sql(source_file="/long/path/t.jsonl")), ["x"]
        )

    def test_source_listing_puts_an_unchunked_drawer_last(self):
        self.insert("single", "2026-09-02T09:00:00.000000", source="/a/t.jsonl")
        self.insert("c0", "2026-09-02T10:00:00.000000", chunk="0", source="/a/t.jsonl")

        self.assertEqual(
            self.run_sql(*wq.build_source_sql(source_file="t.jsonl")), ["c0", "single"]
        )

    def test_a_non_numeric_chunk_index_does_not_error_the_listing(self):
        self.insert("weird", "2026-09-02T10:00:00.000000", chunk="not-a-number",
                    source="/a/t.jsonl")
        self.insert("c0", "2026-09-02T10:00:01.000000", chunk="0", source="/a/t.jsonl")

        self.assertEqual(
            self.run_sql(*wq.build_source_sql(source_file="t.jsonl")), ["c0", "weird"]
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
