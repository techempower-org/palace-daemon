"""SQL builder + keyset cursor for the time-ordered listing (mempalace#500).

Why a separate module rather than more `/list`: `/list`'s documented
contract is "approximates insertion order, not guaranteed", it caps at 100
results and collapses chunk groups through a TTL cache. Exact time
semantics and keyset pagination cannot be bolted on without either
changing that contract or carrying two orderings in one handler.

Two facts read off the production palace on 2026-09-17 shape everything
here:

- **There is no `created_at` column.** The live table is
  `id | document | embedding | metadata | wing | room | doc_tsv`, with no
  index on any time value. Time lives in `metadata->>'filed_at'`.
- **`filed_at` carries two conventions**: 915,562 rows naive host-local,
  5,474 suffixed `Z` (UTC), 34 without fractional seconds, and 2,613 with
  no `filed_at` at all. A text sort and a `::timestamp` sort agree
  perfectly (0 rank differences over 60k rows, two wings) because
  postgres *discards* the `Z` — they are wrong in the same way, which is
  why the agreement is not reassurance. See mempalace#506.

So the ordering key is deliberately pluggable, and both modes are tested:

``wallclock``  compare the raw string, matching the existing contract in
               `mempalace/date_window.py` ("any timezone offset on the
               input is dropped... comparison is therefore wall-clock").
               Consistent with `list --since` / `search --since`; leaves
               the 5,474 `Z` rows up to 7h out of position.
``instant``    normalise to a real instant (`Z` → UTC, naive → the palace
               host's zone). Chronologically correct; disagrees with the
               other surfaces until mempalace#506 moves them too.

Run with::

    cd /home/jp/Projects/palace-daemon
    PYTHONPATH=. venv/bin/python -m pytest tests/test_window_query.py -q
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


def _norm(sql: str) -> str:
    return " ".join(sql.split())


class TestOrderingKey(unittest.TestCase):
    def test_wallclock_mode_compares_the_raw_string(self):
        """Matches date_window.py's contract: offsets dropped, wall-clock."""
        expr = wq.instant_expr("wallclock", "America/Los_Angeles")
        self.assertIn("filed_at", expr)
        self.assertNotIn("AT TIME ZONE", expr)

    def test_instant_mode_normalises_both_conventions(self):
        """Z -> UTC, naive -> the host zone. Both branches must be present."""
        expr = _norm(wq.instant_expr("instant", "America/Los_Angeles"))
        self.assertIn("AT TIME ZONE 'UTC'", expr)
        self.assertIn("AT TIME ZONE 'America/Los_Angeles'", expr)
        self.assertIn("CASE", expr)

    def test_instant_mode_uses_a_zone_name_not_a_fixed_offset(self):
        """A fixed offset is wrong half the year; DST needs a zone name."""
        expr = wq.instant_expr("instant", "America/Los_Angeles")
        self.assertNotIn("-07", expr)
        self.assertNotIn("+07", expr)

    def test_the_zone_name_is_validated_not_interpolated_blindly(self):
        """The zone reaches SQL as a literal, so it must be checked first."""
        with self.assertRaises(ValueError):
            wq.instant_expr("instant", "'; DROP TABLE mempalace_drawers; --")
        with self.assertRaises(ValueError):
            wq.instant_expr("instant", "Not/A/Real/Zone")

    def test_an_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            wq.instant_expr("whatever", "UTC")

    def test_default_mode_is_the_conservative_one(self):
        """Until #506 is settled, the default must match the other surfaces."""
        self.assertEqual(wq.DEFAULT_ORDERING, "wallclock")


class TestBuildWindowSql(unittest.TestCase):
    def build(self, **kw):
        kw.setdefault("wing", "memorypalace")
        return wq.build_window_sql(**kw)

    def test_orders_ascending_by_instant_then_id(self):
        sql, _ = self.build()
        self.assertIn("ORDER BY", _norm(sql))
        self.assertTrue(_norm(sql).rstrip().endswith("LIMIT %s"))
        self.assertIn("ASC, d.id ASC", _norm(sql))

    def test_no_ranking_and_no_vector_reference(self):
        """Unranked by definition — an embedding call is the bug being fixed."""
        sql, _ = self.build()
        low = sql.lower()
        for forbidden in ("embedding", "<=>", "<->", "similarity", "rank", "ts_rank"):
            self.assertNotIn(forbidden, low, f"{forbidden!r} has no place in a time listing")

    def test_wing_filter_is_bound_not_interpolated(self):
        sql, params = self.build(wing="memorypalace")
        self.assertIn("d.wing = %s", _norm(sql))
        self.assertIn("memorypalace", params)

    def test_since_is_inclusive_and_before_is_exclusive(self):
        """Matches tool_list_drawers' documented [since, before) (#1128)."""
        sql, params = self.build(since="2026-09-01", before="2026-09-05")
        norm = _norm(sql)
        self.assertIn(">= %s", norm)
        self.assertIn("< %s", norm)
        self.assertNotIn("<= %s", norm)

    def test_room_and_source_file_filters_are_bound(self):
        sql, params = self.build(room="diary", source_file="/x/y/t.jsonl")
        norm = _norm(sql)
        self.assertIn("d.room = %s", norm)
        self.assertIn("source_file", norm)
        self.assertIn("diary", params)

    def test_source_file_matches_the_basename_too(self):
        """Stored values are absolute paths; users pass what search showed them."""
        _, params = self.build(source_file="transcript.jsonl")
        self.assertTrue(
            any("transcript.jsonl" in str(p) for p in params),
            "the basename the user typed must reach the query",
        )

    def test_page_cap_is_enforced_at_1000(self):
        _, params = self.build(limit=99999)
        self.assertIn(1000, params)
        _, params = self.build(limit=5)
        self.assertIn(5, params)

    def test_limit_below_one_is_clamped_up(self):
        _, params = self.build(limit=0)
        self.assertIn(1, params)

    def test_a_coarse_text_prefilter_widens_the_bounds(self):
        """Index-friendly prefilter; must be wider than the exact bound so a
        Z row near the edge is not dropped before normalisation sees it."""
        sql, params = self.build(since="2026-09-02T00:00:00", before="2026-09-03T00:00:00",
                                 ordering="instant")
        strs = [p for p in params if isinstance(p, str)]
        self.assertTrue(
            any(p.startswith("2026-09-01") for p in strs),
            f"prefilter should reach back a day, got {strs}",
        )
        self.assertTrue(
            any(p.startswith("2026-09-04") for p in strs),
            f"prefilter should reach forward a day, got {strs}",
        )

    def test_wallclock_mode_needs_no_widening(self):
        """Widening exists only to protect normalisation; without it, don't."""
        _, params = self.build(since="2026-09-02T00:00:00", before="2026-09-03T00:00:00",
                               ordering="wallclock")
        strs = [p for p in params if isinstance(p, str)]
        self.assertFalse(any(p.startswith("2026-09-01") for p in strs))

    def test_rows_without_filed_at_cannot_appear(self):
        """Existing contract: a drawer with no filed_at is excluded under a
        bound (tool_list_drawers). 2,613 rows in production."""
        sql, _ = self.build(since="2026-09-01")
        self.assertIn("IS NOT NULL", _norm(sql))


class TestCursor(unittest.TestCase):
    def test_round_trip(self):
        token = wq.encode_cursor("2026-09-02T10:00:00.123456", "drawer_x")
        self.assertEqual(wq.decode_cursor(token), ("2026-09-02T10:00:00.123456", "drawer_x"))

    def test_cursor_is_opaque_rather_than_a_readable_pair(self):
        token = wq.encode_cursor("2026-09-02T10:00:00", "drawer_x")
        self.assertNotIn("drawer_x", token)

    def test_an_id_containing_the_separator_survives(self):
        """Drawer ids are wing/room-derived; never assume they are separator-free."""
        weird = "drawer|with|bars"
        self.assertEqual(wq.decode_cursor(wq.encode_cursor("2026-01-01", weird))[1], weird)

    def test_a_malformed_cursor_raises_rather_than_silently_starting_over(self):
        """Silently restarting a page walk duplicates rows and looks like data."""
        for bad in ("", "not-base64!!", wq._b64("no-separator-here"), "eyJhIjoxfQ=="):
            with self.assertRaises(ValueError, msg=f"{bad!r} should be rejected"):
                wq.decode_cursor(bad)

    def test_the_cursor_predicate_is_a_strict_tuple_comparison(self):
        """(instant, id) > (c_instant, c_id) — not instant > c_instant, which
        would skip every row sharing the boundary timestamp. 4 drawers in the
        production sample shared one filed_at to the microsecond."""
        sql, params = wq.build_window_sql(
            wing="w", cursor=wq.encode_cursor("2026-09-02T10:00:00", "drawer_m")
        )
        norm = _norm(sql)
        self.assertIn("(", norm)
        self.assertIn("> (%s, %s)", norm)
        self.assertIn("drawer_m", params)


class TestSourceSql(unittest.TestCase):
    def test_orders_by_chunk_index_then_filed_then_id(self):
        """#502 wants "in order" — chunk order within a file, not time order.

        Asserted on the ORDER BY clause only: ``d.id`` also appears in the
        SELECT list, so searching the whole statement compares against the
        wrong occurrence (it did, on the first draft of this test).
        """
        sql, _ = wq.build_source_sql(source_file="t.jsonl")
        order_by = _norm(sql).split("ORDER BY", 1)[1]
        self.assertIn("chunk_index", order_by)
        self.assertLess(order_by.index("chunk_index"), order_by.index("d.id"))
        self.assertLess(order_by.index("chunk_index"), order_by.index("filed_at"))

    def test_chunk_index_sorts_numerically_not_lexically(self):
        """Lexically, chunk 10 sorts before chunk 2 — the exact bug #502 exists
        to avoid ("the drawers from THAT file, in order")."""
        sql, _ = wq.build_source_sql(source_file="t.jsonl")
        order_by = _norm(sql).split("ORDER BY", 1)[1]
        self.assertIn("::int", order_by, "chunk_index must be cast before sorting")

    def test_a_non_numeric_chunk_index_does_not_break_the_cast(self):
        """A stray non-numeric chunk_index must not error the whole listing."""
        sql, _ = wq.build_source_sql(source_file="t.jsonl")
        self.assertIn("~ '^[0-9]+$'", _norm(sql))

    def test_unchunked_drawers_sort_last_rather_than_vanishing(self):
        sql, _ = wq.build_source_sql(source_file="t.jsonl")
        self.assertIn("NULLS LAST", _norm(sql).split("ORDER BY", 1)[1])

    def test_source_file_is_required(self):
        with self.assertRaises(ValueError):
            wq.build_source_sql(source_file="")

    def test_wing_is_optional(self):
        sql, params = wq.build_source_sql(source_file="t.jsonl")
        self.assertNotIn("d.wing = %s", _norm(sql))
        sql, params = wq.build_source_sql(source_file="t.jsonl", wing="w")
        self.assertIn("d.wing = %s", _norm(sql))

    def test_no_ranking_here_either(self):
        sql, _ = wq.build_source_sql(source_file="t.jsonl")
        self.assertNotIn("embedding", sql.lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestNoAccidentalPlaceholders(unittest.TestCase):
    """psycopg parses `%X` in query TEXT as a placeholder.

    `LIKE '%Z'` in the instant-ordering expression raised
    "only '%s', '%b', '%t' are allowed as placeholders, got '%Z'" — a
    runtime error armed to fire the day the ordering default flips. No
    assertion on SQL text could see it; the live ordering test did. This
    guards the whole builder against the next one.
    """

    def _assert_only_valid_placeholders(self, sql):
        import re

        bad = [m.group(0) for m in re.finditer(r"%(?!s\b|%)(.)", sql)]
        self.assertEqual(bad, [], f"bare % in query text: {bad} in {sql!r}")

    def test_window_sql_has_no_stray_percent_in_either_mode(self):
        for ordering, tz in (("wallclock", "UTC"), ("instant", "America/Los_Angeles")):
            sql, _ = wq.build_window_sql(
                wing="w", room="r", since="2026-09-01", before="2026-09-05",
                source_file="t.jsonl", cursor=wq.encode_cursor("2026-09-02", "d"),
                ordering=ordering, tz_name=tz,
            )
            self._assert_only_valid_placeholders(sql)

    def test_source_sql_has_no_stray_percent(self):
        sql, _ = wq.build_source_sql(source_file="t.jsonl", wing="w")
        self._assert_only_valid_placeholders(sql)

    def test_the_wildcard_lives_in_the_PARAM_not_the_query_text(self):
        """Basename matching needs a wildcard; it belongs in the bound value."""
        sql, params = wq.build_source_sql(source_file="t.jsonl")
        self.assertNotIn("%/", sql)
        self.assertTrue(any(str(p).startswith("%/") for p in params))
