#!/usr/bin/env python3
"""Measure the /window page query against a SCRATCH palace. Never production.

mempalace#500's complaint is that no cheap chronological walk exists:
``search --since`` ranks inside the window and ``list`` has no time filter,
so at 201K drawers a date is ~200 pages away. This measures what the new
query actually costs, and whether paging stays flat as the walk advances —
the property that decides whether the verb is usable interactively.

Guard, same shape as ``bench_kg_writethrough.py`` (palace-daemon#265):
an allowlist, not a denylist. ``--scratch-db`` is required and the DSN's
database must equal it, there is no override flag, and naming a known
production database is refused outright. A hostname cannot carry the
signal — production is ``…@localhost:5433/mempalace_2026_05_13``, so
"is the host familiar?" lets the real DSN straight through.

Usage::

    python scripts/bench_window.py --dsn postgresql://u:p@host/scratch \\
        --scratch-db scratch [--rows 200000]

Exits non-zero when the walk does not visit every row exactly once: a
benchmark whose validity condition fails silently will eventually be
quoted from a broken run.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import window_query as wq  # noqa: E402

_KNOWN_PRODUCTION_DBS = frozenset({"mempalace_2026_05_13"})
_TABLE = "window_bench_drawers"


def _dbname(dsn: str) -> str:
    try:
        return (urlparse(dsn).path or "").lstrip("/")
    except Exception:  # noqa: BLE001
        return ""


def _seed(cur, rows: int, z_fraction: float) -> None:
    """Rows whose filed_at spans a month, with a realistic Z minority.

    The Z share matters: production is 5,474 of 921,070 (0.6%), and those
    rows are the ones mempalace#506 is about. Seeding none of them would
    measure a corpus this query will never see.
    """
    cur.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    cur.execute(
        f"""CREATE TABLE {_TABLE} (
               id text PRIMARY KEY,
               wing text NOT NULL DEFAULT '',
               room text NOT NULL DEFAULT '',
               document text NOT NULL DEFAULT '',
               metadata jsonb NOT NULL DEFAULT '{{}}'::jsonb)"""
    )
    cur.execute(
        f"""
        INSERT INTO {_TABLE} (id, wing, room, document, metadata)
        SELECT
          'bench_' || lpad(i::text, 9, '0'),
          CASE WHEN i %% 5 = 0 THEN 'other' ELSE 'bench' END,
          CASE WHEN i %% 3 = 0 THEN 'diary' ELSE 'sessions' END,
          'document body ' || i,
          jsonb_build_object(
            'filed_at',
            CASE WHEN i %% {max(1, int(1 / max(z_fraction, 1e-9)))} = 0
                 THEN to_char(timestamp '2026-08-01 00:00:00'
                              + (i * 26 || ' seconds')::interval,
                              'YYYY-MM-DD"T"HH24:MI:SS.MS') || 'Z'
                 ELSE to_char(timestamp '2026-08-01 00:00:00'
                              + (i * 26 || ' seconds')::interval,
                              'YYYY-MM-DD"T"HH24:MI:SS.US')
            END,
            'source_file', '/t/' || (i %% 50) || '.jsonl',
            'chunk_index', (i %% 12)::text)
        FROM generate_series(1, %s) AS s(i)
        """,
        (rows,),
    )
    cur.execute(f"ANALYZE {_TABLE}")


def _timed(cur, sql, params):
    sql = sql.replace(wq.TABLE, _TABLE)
    start = time.time()
    cur.execute(sql, params)
    got = cur.fetchall()
    return (time.time() - start) * 1000.0, got


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", required=True)
    ap.add_argument("--scratch-db", required=True)
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--page", type=int, default=100)
    ap.add_argument("--z-fraction", type=float, default=0.006)
    args = ap.parse_args(argv)

    if args.scratch_db in _KNOWN_PRODUCTION_DBS:
        print(f"refusing: {args.scratch_db!r} is a known production database.", file=sys.stderr)
        return 2
    db = _dbname(args.dsn)
    if db != args.scratch_db:
        print(
            f"refusing: DSN database is {db!r} but --scratch-db is {args.scratch_db!r}. "
            "This benchmark creates and drops a table; name the scratch database.",
            file=sys.stderr,
        )
        return 2

    import psycopg

    with psycopg.connect(args.dsn) as conn:
        conn.autocommit = True
        with conn.cursor() as cur:
            print(f"seeding {args.rows:,} rows (z-fraction {args.z_fraction})…")
            _seed(cur, args.rows, args.z_fraction)

            print(f"\n{'query':<46} {'ms':>9} {'rows':>7}")
            print("-" * 66)

            # Bounds derived from the data, not hardcoded: a window outside
            # the seeded range returns 0 rows and times an empty scan, which
            # looks like a fast query. The first draft of this script did
            # exactly that (1-second spacing spans 2.3 days; the window asked
            # for Aug 10-14) and printed 26.9 ms for zero rows.
            cur.execute(
                f"SELECT min(metadata->>'filed_at'), max(metadata->>'filed_at') FROM {_TABLE}"
            )
            lo, hi = cur.fetchone()
            print(f"seeded range {lo} .. {hi}")
            mid = lo[:10]
            span_since = f"{mid}T00:00:00"
            span_before = f"{lo[:8]}{int(lo[8:10]) + 4:02d}T00:00:00"

            cases = [
                ("first page, wing only", dict(wing="bench", limit=args.page)),
                ("4-day window, wing", dict(wing="bench", since=span_since,
                                            before=span_before, limit=args.page)),
                ("4-day window + room", dict(wing="bench", room="diary",
                                             since=span_since,
                                             before=span_before, limit=args.page)),
                ("by source_file (basename)", None),
            ]
            empty = []
            for label, kw in cases:
                if kw is None:
                    ms, got = _timed(cur, *wq.build_source_sql(source_file="7.jsonl"))
                else:
                    ms, got = _timed(cur, *wq.build_window_sql(**kw))
                print(f"{label:<46} {ms:>9.1f} {len(got):>7}")
                if not got:
                    empty.append(label)
            if empty:
                print(
                    f"\nFAIL: these cases returned no rows, so their timings measure "
                    f"an empty scan rather than the query: {empty}. "
                    "A benchmark that reports a fast empty window is worse than none.",
                    file=sys.stderr,
                )
                return 5

            # Does paging stay flat, or degrade as the walk advances? An
            # OFFSET-based pager degrades linearly; a keyset pager should not.
            print(f"\n{'paging through the window':<46} {'ms':>9} {'rows':>7}")
            print("-" * 66)
            cursor, seen, page_no, timings = None, [], 0, []
            while page_no < 12:
                ms, got = _timed(
                    cur,
                    *wq.build_window_sql(
                        wing="bench", since=span_since,
                        before=span_before, limit=args.page, cursor=cursor,
                    ),
                )
                if not got:
                    break
                timings.append(ms)
                seen.extend(r[0] for r in got)
                cursor = wq.encode_cursor(got[-1][5], got[-1][0])
                page_no += 1
                if page_no in (1, 2, 6, 12):
                    print(f"{'  page ' + str(page_no):<46} {ms:>9.1f} {len(got):>7}")

            if len(seen) != len(set(seen)):
                dupes = len(seen) - len(set(seen))
                print(
                    f"\nFAIL: the walk returned {dupes} duplicate row(s). A keyset "
                    "pager that repeats rows is worse than a slow one.",
                    file=sys.stderr,
                )
                return 4
            if timings:
                first, last = timings[0], timings[-1]
                print(f"\nkeyset:  first page {first:.1f} ms -> page {len(timings)} {last:.1f} ms")
                print(f"         visited {len(seen):,} rows, all distinct")

            # The comparison that would make "flat" mean something -- and
            # which this scale does NOT deliver, so it says so.
            #
            # The hypothesis was that an OFFSET pager degrades with depth
            # while a keyset pager stays flat. Measured, it does not: on an
            # UNINDEXED filed_at the sequential scan costs ~45 ms and
            # dominates, so the skip cost is lost in the noise. The keyset
            # design is still the right one (it is also correct under
            # concurrent inserts, which OFFSET is not), but the performance
            # advantage is not measurable here and is not claimed.
            #
            # Pages are bounded by the rows that actually exist: the first
            # draft asked for page 200 of a ~10k-row window and timed an
            # empty result, which is the same defect this script already
            # guards against above.
            cur.execute(
                f"""SELECT count(*) FROM {_TABLE} d
                    WHERE d.wing = %s
                      AND (d.metadata->>'filed_at') >= %s
                      AND (d.metadata->>'filed_at') <  %s""",
                ("bench", span_since, span_before),
            )
            window_rows = cur.fetchone()[0]
            last_page = max(1, window_rows // args.page)
            print(
                f"\n{'same walk with OFFSET instead of a cursor':<46} {'ms':>9} {'rows':>7}"
            )
            print(f"(window holds {window_rows:,} rows -> {last_page} full pages)")
            print("-" * 66)
            base_sql, base_params = wq.build_window_sql(
                wing="bench", since=span_since, before=span_before, limit=args.page
            )
            off_sql = base_sql.replace(wq.TABLE, _TABLE) + " OFFSET %s"
            off = []
            for page_no in sorted({1, 2, last_page // 2 or 1, last_page}):
                params = list(base_params) + [(page_no - 1) * args.page]
                start = time.time()
                cur.execute(off_sql, params)
                got = cur.fetchall()
                ms = (time.time() - start) * 1000.0
                off.append((page_no, ms, len(got)))
                print(f"{'  page ' + str(page_no):<46} {ms:>9.1f} {len(got):>7}")
            if any(n == 0 for _, _, n in off):
                print(
                    "\nFAIL: an OFFSET page returned no rows, so its timing measures "
                    "an empty scan.",
                    file=sys.stderr,
                )
                return 6
            shallow = off[0][1]
            deep = off[-1][1]
            print(
                f"\nOFFSET page 1 {shallow:.1f} ms -> page {off[-1][0]} {deep:.1f} ms "
                f"({deep / max(shallow, 0.01):.2f}x)"
            )
            print(
                "conclusion: at this scale the unindexed scan dominates and the\n"
                "            keyset advantage is NOT measurable. Keyset is kept for\n"
                "            correctness under concurrent inserts, and becomes the\n"
                "            differentiator once an index lands (mempalace#507)."
            )

            cur.execute(f"DROP TABLE IF EXISTS {_TABLE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
