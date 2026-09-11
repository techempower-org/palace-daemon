"""The `max_source_mtime` cast guard, tested for behaviour not for substrings.

`fast_mcp_mined` casts `metadata->>'source_mtime'` to `double precision`
inside a `CASE WHEN` so one malformed value cannot abort the aggregation and
blind the check for every file in a wing. The original test asserted that the
SQL string *contained* "CASE WHEN", which would pass against a guard that
guards nothing — the weaker instrument for the exact property that matters.

Two tests here, in increasing strength:

* the aggregation's Python side, through a fake cursor that returns rows, so
  the None handling is exercised rather than inspected;
* the predicate itself against a REAL Postgres when a DSN is available,
  because only Postgres can say what its own cast accepts. Skipped without
  one, and the skip is named so a green run is not mistaken for coverage.

Run with::

    MEMPALACE_POSTGRES_DSN=... python -m pytest tests/test_mined_mtime_guard.py -q
"""
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import main  # noqa: E402
import postgres  # noqa: E402

from tests.test_daemon_native_tools import _FakeConn, _FakeCursor  # noqa: E402

# A real mtime is ~10 digits plus a fraction; the cast gives out around 308.
_GUARD_MAX_LEN = 20
_GUARD_RE = r"^[0-9]+(\.[0-9]+)?$"

_CASES = [
    # (label, stored value, must the guard let it through?)
    ("a real mtime", "1789069053.828", True),
    ("an integral mtime", "1789069053", True),
    ("NaN", "NaN", False),
    ("Infinity", "Infinity", False),
    ("-Infinity", "-Infinity", False),
    ("empty", "", False),
    ("prose", "yesterday", False),
    ("scientific notation", "1e400", False),
    ("a 400-digit integer", "1" + "0" * 400, False),
]


class TestMtimeAggregationBehaviour(unittest.TestCase):
    """The Python side, exercised through rows rather than read as a string."""

    def _run(self, rows):
        cur = _FakeCursor([("FROM mempalace_drawers", rows)])
        with patch.object(postgres, "postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            return main._fast_mcp_mined({})

    def test_a_null_mtime_stays_none_and_does_not_become_zero(self):
        """NULL is what the guard produces for a rejected value. Reading it as
        0 would date every such file to 1970 and report it fresh forever."""
        result = self._run([("w", "/p/a.md", 3, None)])
        source = result["sources_by_wing"]["w"]["sources"][0]
        self.assertIsNone(source["max_source_mtime"])
        self.assertNotEqual(source["max_source_mtime"], 0)

    def test_a_present_mtime_is_a_float(self):
        result = self._run([("w", "/p/a.md", 3, 1789069053.828)])
        source = result["sources_by_wing"]["w"]["sources"][0]
        self.assertIsInstance(source["max_source_mtime"], float)
        self.assertEqual(source["max_source_mtime"], 1789069053.828)

    def test_one_unusable_source_does_not_blind_its_neighbours(self):
        """The whole point of the CASE WHEN: a bad row is one NULL, not an
        exception that takes the wing's other files with it."""
        result = self._run(
            [("w", "/p/bad.md", 1, None), ("w", "/p/good.md", 2, 1789069053.828)]
        )
        by_src = {s["source_file"]: s for s in result["sources_by_wing"]["w"]["sources"]}
        self.assertIsNone(by_src["/p/bad.md"]["max_source_mtime"])
        self.assertEqual(by_src["/p/good.md"]["max_source_mtime"], 1789069053.828)

    def test_the_guard_carries_both_halves(self):
        """A regex alone lets a 400-digit run through; a length bound alone
        lets 'NaN' through. Both must be in the emitted SQL."""
        cur = _FakeCursor([("FROM mempalace_drawers", [("w", "/x", 1, None)])])
        with patch.object(postgres, "postgres_dsn", return_value="x"), \
             patch("psycopg2.connect", return_value=_FakeConn(cur)):
            main._fast_mcp_mined({})
        sql = [e[0] for e in cur.executed if "FROM mempalace_drawers" in e[0]][0]
        self.assertIn("~", sql)
        self.assertIn("length(", sql)
        self.assertIn("<= 20", sql)


def _dsn():
    return os.environ.get("MEMPALACE_POSTGRES_DSN") or os.environ.get("MEMPALACE_PG_DSN")


@unittest.skipUnless(_dsn(), "no MEMPALACE_POSTGRES_DSN — the cast's own engine is unavailable")
class TestMtimeGuardAgainstRealPostgres(unittest.TestCase):
    """Only Postgres can say what Postgres accepts.

    The unit tests above cannot: a fake cursor never parses the SQL, so a
    guard that admits an overflowing value looks identical to one that does
    not until it aborts a real aggregation.
    """

    @classmethod
    def setUpClass(cls):
        import psycopg

        cls._conn = psycopg.connect(_dsn(), autocommit=True)

    @classmethod
    def tearDownClass(cls):
        cls._conn.close()

    def _guard_admits(self, value: str) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT %s ~ %s AND length(%s) <= %s",
                (value, _GUARD_RE, value, _GUARD_MAX_LEN),
            )
            return bool(cur.fetchone()[0])

    def _cast_survives(self, value: str) -> bool:
        with self._conn.cursor() as cur:
            try:
                cur.execute("SELECT (%s)::double precision", (value,))
                cur.fetchone()
                return True
            except Exception:
                return False

    def test_the_guard_admits_exactly_what_the_cast_survives(self):
        for label, value, expected in _CASES:
            with self.subTest(case=label):
                admitted = self._guard_admits(value)
                self.assertEqual(
                    admitted, expected, f"{label}: guard admitted={admitted}"
                )
                if admitted:
                    self.assertTrue(
                        self._cast_survives(value),
                        f"{label}: guard admitted a value the cast cannot take",
                    )

    def test_the_unguarded_cast_really_would_have_failed(self):
        """A positive control: without this, the guard's tests prove nothing.

        `NaN` and `Infinity` cast fine and would reach JSON as invalid
        literals; the 400-digit integer raises outright. Both are the
        failures the guard exists for, so both must be demonstrable.
        """
        self.assertTrue(self._cast_survives("NaN"), "NaN casts fine — that IS the hazard")
        self.assertTrue(self._cast_survives("Infinity"))
        self.assertFalse(
            self._cast_survives("1" + "0" * 400), "a 400-digit value must overflow"
        )
