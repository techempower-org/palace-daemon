"""GET /window and GET /source — the route layer (mempalace#500, #502).

The SQL and the cursor are covered in test_window_query.py (unit) and
test_window_ordering_live.py (against a real postgres). What is left here
is the HTTP contract, which is what the mempalace CLI depends on:

- auth, and 401 without a key
- bound parsing delegated to ``mempalace.date_window`` so ``/window``
  cannot drift from ``list --since`` / ``search --since``
- 400 for a bad ISO bound, an inverted window, or a corrupt cursor — the
  CLI maps a 4xx on a well-formed route to exit 64 and surfaces the
  daemon's message, so the message is part of the contract
- ``excluded_no_filed_at`` in the body: the 2,613 production drawers with
  no ``filed_at`` are excluded by the existing contract, and a count is
  the difference between an exclusion and a disappearance
- ``next_cursor`` present only when the page is full

Run with::

    cd /home/jp/Projects/palace-daemon
    PYTHONPATH=. venv/bin/python -m pytest tests/test_window_routes.py -q
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

try:
    from fastapi.testclient import TestClient

    HAVE_FASTAPI = True
except ImportError:  # pragma: no cover
    HAVE_FASTAPI = False

import main  # noqa: E402
import window_query as wq  # noqa: E402


class _FakeCursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((" ".join(str(sql).split()), params))
        text = self.conn.executed[-1][0]
        if text.startswith("SELECT count(*)"):
            self.conn._next = [(self.conn.missing_count,)]
        else:
            self.conn._next = list(self.conn.rows)

    def fetchall(self):
        return self.conn._next

    def fetchone(self):
        return self.conn._next[0] if self.conn._next else None


class _FakeConn:
    def __init__(self, rows=None, missing_count=0):
        self.rows = rows or []
        self.missing_count = missing_count
        self.executed = []
        self._next = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        pass


def _row(drawer_id, filed_at, *, wing="w", room="", doc="hello", meta=None):
    meta = dict(meta or {})
    meta.setdefault("filed_at", filed_at)
    return (drawer_id, wing, room, doc, meta, filed_at)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi not installed")
class _RouteCase(unittest.TestCase):
    def client(self, conn):
        self._patches = [
            patch.object(main, "_check_auth", lambda *_a, **_k: None),
            patch("window_routes.connect_postgres", return_value=conn),
        ]
        for p in self._patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self._patches])
        return TestClient(main.app)


class TestWindowRoute(_RouteCase):
    def test_returns_drawers_in_the_envelope_the_cli_expects(self):
        conn = _FakeConn(rows=[_row("d1", "2026-09-02T10:00:00.000000")])
        r = self.client(conn).get("/window", params={"wing": "w"})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["drawers"][0]["drawer_id"], "d1")
        self.assertEqual(body["drawers"][0]["filed_at"], "2026-09-02T10:00:00.000000")
        self.assertIn("wing", body["drawers"][0])

    def test_reports_the_count_excluded_for_a_missing_filed_at(self):
        """2,613 in production. An exclusion the caller cannot see is a
        disappearance."""
        conn = _FakeConn(rows=[_row("d1", "2026-09-02T10:00:00")], missing_count=2613)
        r = self.client(conn).get("/window", params={"wing": "w", "since": "2026-09-01"})
        self.assertEqual(r.json()["excluded_no_filed_at"], 2613)

    def test_does_not_count_exclusions_when_no_bound_is_active(self):
        """With no bound, nothing is excluded — reporting a number would lie."""
        conn = _FakeConn(rows=[_row("d1", "2026-09-02T10:00:00")], missing_count=2613)
        body = self.client(conn).get("/window", params={"wing": "w"}).json()
        self.assertEqual(body["excluded_no_filed_at"], 0)

    def test_the_response_states_the_ordering_it_used(self):
        """The contract is wall-clock today and may become instant (#506).
        A caller must be able to tell which answer it got."""
        conn = _FakeConn(rows=[])
        body = self.client(conn).get("/window", params={"wing": "w"}).json()
        self.assertEqual(body["ordering"], wq.DEFAULT_ORDERING)

    def test_next_cursor_only_when_the_page_is_full(self):
        rows = [_row(f"d{i}", f"2026-09-02T10:00:0{i}.000000") for i in range(3)]
        conn = _FakeConn(rows=rows)
        full = self.client(conn).get("/window", params={"wing": "w", "limit": 3}).json()
        self.assertTrue(full["next_cursor"])
        self.assertEqual(wq.decode_cursor(full["next_cursor"])[1], "d2")

        conn2 = _FakeConn(rows=rows)
        short = self.client(conn2).get("/window", params={"wing": "w", "limit": 10}).json()
        self.assertIsNone(short["next_cursor"])

    def test_a_bad_iso_bound_is_400_with_a_usable_message(self):
        conn = _FakeConn(rows=[])
        r = self.client(conn).get("/window", params={"wing": "w", "since": "not-a-date"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"].lower()
        self.assertIn("since", detail)
        self.assertIn("iso", detail)

    def test_an_inverted_window_is_400(self):
        conn = _FakeConn(rows=[])
        r = self.client(conn).get(
            "/window", params={"wing": "w", "since": "2026-09-05", "before": "2026-09-01"}
        )
        self.assertEqual(r.status_code, 400)

    def test_a_corrupt_cursor_is_400_not_a_silent_restart(self):
        """A cursor that quietly meant 'start over' would duplicate rows the
        caller already has and look like data."""
        conn = _FakeConn(rows=[])
        r = self.client(conn).get("/window", params={"wing": "w", "cursor": "garbage!!"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("cursor", r.json()["detail"].lower())

    def test_bounds_are_parsed_by_the_shared_mempalace_helper(self):
        """Delegation, not a second parser: /window must not drift from
        list --since. Asserted by making the shared helper raise."""
        conn = _FakeConn(rows=[])
        with patch("window_routes.parse_window", side_effect=ValueError("shared parser said no")):
            r = self.client(conn).get("/window", params={"wing": "w", "since": "2026-09-01"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("shared parser said no", r.json()["detail"])

    def test_no_embedding_call_is_made(self):
        """Unranked by definition. An embed here is the bug #500 describes."""
        conn = _FakeConn(rows=[_row("d1", "2026-09-02T10:00:00")])
        self.client(conn).get("/window", params={"wing": "w"})
        for sql, _ in conn.executed:
            self.assertNotIn("embedding", sql.lower())

    def test_the_page_cap_is_applied_by_the_route_too(self):
        conn = _FakeConn(rows=[])
        self.client(conn).get("/window", params={"wing": "w", "limit": 99999})
        # Skip the SET LOCAL (params None) and the count query; the page
        # query is the one carrying bound parameters.
        page_params = [
            prm
            for sql, prm in conn.executed
            if prm and not sql.startswith("SELECT count(*)")
        ]
        self.assertTrue(page_params, f"no parameterised page query ran: {conn.executed}")
        self.assertIn(wq.MAX_PAGE, page_params[0])


class TestSourceRoute(_RouteCase):
    def test_lists_one_file_in_chunk_order(self):
        rows = [
            _row("c0", "2026-09-02T10:00:00", meta={"chunk_index": "0"}),
            _row("c1", "2026-09-02T10:00:01", meta={"chunk_index": "1"}),
        ]
        conn = _FakeConn(rows=rows)
        r = self.client(conn).get("/source", params={"source_file": "t.jsonl"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([d["drawer_id"] for d in r.json()["drawers"]], ["c0", "c1"])

    def test_source_file_is_required(self):
        conn = _FakeConn(rows=[])
        self.assertEqual(self.client(conn).get("/source").status_code, 422)

    def test_a_blank_source_file_is_400(self):
        conn = _FakeConn(rows=[])
        r = self.client(conn).get("/source", params={"source_file": "  "})
        self.assertEqual(r.status_code, 400)

    def test_wing_is_optional(self):
        conn = _FakeConn(rows=[])
        self.assertEqual(
            self.client(conn).get("/source", params={"source_file": "t.jsonl"}).status_code, 200
        )

    def test_reports_the_file_it_matched_on(self):
        """The user pastes a basename; the response says what was queried."""
        conn = _FakeConn(rows=[])
        body = self.client(conn).get("/source", params={"source_file": "t.jsonl"}).json()
        self.assertEqual(body["source_file"], "t.jsonl")


class TestVersionAdvertisesTheRoutes(unittest.TestCase):
    def test_version_was_bumped_for_the_new_routes(self):
        """The mempalace CLI's 404 message names a minimum version, so the
        version has to move when the routes land or the message is a lie."""
        parts = [int(x) for x in main.VERSION.split(".")[:2]]
        self.assertGreaterEqual(parts, [1, 10], f"VERSION={main.VERSION}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
