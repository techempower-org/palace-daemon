"""mempalace_list_hallways answered from the hallway table (#255).

#239's third item could not be fixed with the #49/#231 fast-intercept
pattern, because there was nothing to intercept *to*: the hallway store was
one JSON file and ``list_hallways`` loaded all of it on every call, so the
``wing`` argument bought nothing. Measured on familiar, 2026-09-10:

    ~/.mempalace/hallways.json   479,034,503 bytes -> 1,041,537,215 bytes
                                 796,831 records, 21 wings, one week apart
    postgres backing             none

That is a memory hazard rather than only a timeout. ``json.load`` of a 1 GB
array materializes several GB of heap, and this daemon runs under a
deliberate ``MemoryMax=2G`` cap already sitting at 98.4% (anon 2014.9 MB of
2048 MB, memory.peak 2049.2 MB). The call was never fired at production
precisely because the plausible outcome was the first OOM kill.

mempalace#442 gives us the table. This is the ``fast_mcp_*`` handler that
falls out of it — and it is bounded by default, because 797K records is not
a sane MCP payload at any speed.

Run with::

    cd /home/jp/Projects/palace-daemon
    venv/bin/python -m pytest tests/test_hallways_fast_intercept.py -v
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import daemon_tools  # noqa: E402
import postgres  # noqa: E402

from tests.test_daemon_native_tools import _FakeConn, _FakeCursor  # noqa: E402


_ROW = (
    "hallway_kiyo_Aya_Lumi_deadbeef",
    "kiyo",
    "Aya",
    "Lumi",
    47,
    ["diary", "letters"],
    "Aya <-> Lumi",
    "2026-09-10T00:00:00+00:00",
    "auto",
    {"strength": 0.7, "access_count": 3},
    {},
)


def _run(arguments, rows=None, script=None):
    cursor = _FakeCursor(script or [("FROM mempalace_hallways", rows or [_ROW])])
    with patch.object(postgres, "postgres_dsn", return_value="postgres://x"), patch(
        "psycopg2.connect", return_value=_FakeConn(cursor)
    ):
        result = daemon_tools.fast_mcp_list_hallways(arguments)
    return result, cursor


class TestListHallwaysFastIntercept(unittest.TestCase):
    def test_returns_records_from_the_table(self):
        result, _ = _run({"wing": "kiyo"})
        self.assertEqual(result["hallways"][0]["entity_a"], "Aya")
        self.assertEqual(result["hallways"][0]["co_occurrence_count"], 47)

    def test_wing_filter_is_sql_not_a_python_comprehension(self):
        """The whole point of the storage change: a scoped call scans less."""
        _, cursor = _run({"wing": "kiyo"})
        sql, params = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertIn("WHERE wing = %s", " ".join(sql.split()))
        self.assertEqual(params[0], "kiyo")

    def test_unscoped_calls_are_bounded_by_default(self):
        """797K records is not a payload. Never return the store.

        The bound sent to postgres is one row *past* the page: see
        ``test_truncation_is_detected_not_guessed``.
        """
        _, cursor = _run({})
        sql, params = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertIn("LIMIT %s", sql)
        self.assertEqual(params[-1], daemon_tools.HALLWAY_DEFAULT_LIMIT + 1)

    def test_caller_limit_is_honoured_but_capped(self):
        """A caller asking for everything must not be able to reproduce #255."""
        _, cursor = _run({"limit": 10})
        _, params = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertEqual(params[-1], 11)

        _, cursor = _run({"limit": 10_000_000})
        _, params = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertEqual(params[-1], daemon_tools.HALLWAY_MAX_LIMIT + 1)

    def test_the_returned_page_never_exceeds_the_limit(self):
        """The probe row is for detection only; it must not reach the caller."""
        result, _ = _run({"wing": "kiyo", "limit": 2}, rows=[_ROW] * 3)
        self.assertEqual(len(result["hallways"]), 2)

    def test_truncation_is_detected_not_guessed(self):
        """A silently truncated list reads as 'that is all of them'.

        A page that happens to come back exactly full is indistinguishable
        from the end of the list, so the query asks for one row more than it
        will return and reports the difference.
        """
        # Three rows available, two requested -> there is more.
        result, _ = _run({"wing": "kiyo", "limit": 2}, rows=[_ROW] * 3)
        self.assertTrue(result["truncated"])
        self.assertEqual(result["limit"], 2)

        # Exactly a full page and nothing beyond it -> not truncated.
        result, _ = _run({"wing": "kiyo", "limit": 3}, rows=[_ROW] * 3)
        self.assertFalse(result["truncated"])

        result, _ = _run({"wing": "kiyo", "limit": 10}, rows=[_ROW])
        self.assertFalse(result["truncated"])

    def test_offset_is_passed_through_for_paging(self):
        _, cursor = _run({"wing": "kiyo", "offset": 100})
        sql, params = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertIn("OFFSET %s", sql)
        self.assertIn(100, params)

    def test_ordered_by_co_occurrence_so_the_first_page_is_the_strongest(self):
        _, cursor = _run({"wing": "kiyo"})
        sql, _ = [e for e in cursor.executed if "mempalace_hallways" in e[0]][0]
        self.assertIn("ORDER BY co_occurrence_count DESC", " ".join(sql.split()))

    def test_statement_timeout_is_set(self):
        """Same guard the other fast tools use; a scan must not hold /mcp."""
        _, cursor = _run({"wing": "kiyo"})
        self.assertTrue(any("statement_timeout" in sql for sql, _ in cursor.executed))

    def test_missing_table_is_explained_not_an_opaque_error(self):
        """Before the migration runs the table does not exist yet.

        Returning a bare [] would be indistinguishable from "this palace has
        no hallways", and the operator would have no idea the migration is
        the missing step.
        """
        from psycopg2 import errors as pg_errors

        class _RaisingCursor(_FakeCursor):
            def execute(self, sql, params=None):
                self.executed.append((sql, params))
                if "mempalace_hallways" in sql:
                    raise pg_errors.UndefinedTable("relation does not exist")

        cursor = _RaisingCursor([])
        with patch.object(postgres, "postgres_dsn", return_value="postgres://x"), patch(
            "psycopg2.connect", return_value=_FakeConn(cursor)
        ):
            result = daemon_tools.fast_mcp_list_hallways({"wing": "kiyo"})

        self.assertEqual(result["hallways"], [])
        self.assertIn("migrate_hallways", result.get("note", ""))

    def test_registered_for_dispatch_and_discovery(self):
        """Callable *and* visible: an unlisted tool defeats the handshake."""
        self.assertIs(
            daemon_tools.DAEMON_NATIVE_TOOLS["mempalace_list_hallways"],
            daemon_tools.fast_mcp_list_hallways,
        )
        names = [d["name"] for d in daemon_tools.DAEMON_NATIVE_TOOL_DESCRIPTORS]
        self.assertIn("mempalace_list_hallways", names)

    def test_descriptor_advertises_the_paging_arguments(self):
        descriptor = next(
            d
            for d in daemon_tools.DAEMON_NATIVE_TOOL_DESCRIPTORS
            if d["name"] == "mempalace_list_hallways"
        )
        props = descriptor["inputSchema"]["properties"]
        self.assertIn("wing", props)
        self.assertIn("limit", props)
        self.assertIn("offset", props)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
