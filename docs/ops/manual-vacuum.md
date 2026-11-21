# Manual VACUUM, REINDEX and index builds on the palace database

## The rule

**The palace role's `statement_timeout` is 30 s.** Every session of that role inherits it, including one you open
by hand to maintain the database. A `VACUUM`, `REINDEX`, `CREATE INDEX CONCURRENTLY` or `VACUUM FULL` on a large table
runs far longer than that, so postgres cancels it with `ERROR: canceling statement due to statement timeout`.

**So, first in that session:**

    SET statement_timeout = 0;      -- this session only
    SET lock_timeout = '60s';       -- never queue for long behind an exclusive lock

- Use `SET`, never `ALTER ROLE … SET statement_timeout`. The 30 s protects the daemon from runaway queries, and the
  daemon uses the same role.
- `SET` lasts for the session. With `psql -c`, put each statement in its own `-c` in a single call:
  `psql … -c "SET statement_timeout = 0" -c "VACUUM (ANALYZE) …"`. Two separate `psql` calls are two sessions, and
  the second one has the 30 s again.
- Read it back with `SHOW statement_timeout` (it should say `0`) before starting anything long.

A cancelled `VACUUM` is harmless: it changes nothing it did not finish. The cost is only the lost time.

## Run it

`scripts/checks/palace_vacuum.py [schema.table …]` (default `public.mempalace_drawers`) does all of the above. It
lifts the timeout for its own session, reads it back, and refuses to start if it is still set. It refuses any name
that is not a plain `schema.table`. Then it runs `VACUUM (ANALYZE)` on each table and writes `CHECK palace-vacuum` (see
[CHECKS.md](../CHECKS.md)): `warn` if a table still has autovacuum switched off in its reloptions. AGE graph tables
keep their case: `mempalace_kg.MENTIONS`.

**Run it at idle I/O.** The palace host's standing rule is that every heavy read runs at idle I/O class and nice 15.
For a database backend, `ionice` on the client does nothing; the reading is done by the postgres backend inside the
container. Find the backend's host pid (its process title is `postgres: <role> <db> [local] VACUUM`), then
`ionice -c3 -p <pid>` and `renice -n 15 -p <pid>`. Check it with `ionice -p <pid>`, which should print `idle`.

**Not between 01:00 and 03:00:** the nightly dump runs at 02:15.

## Before a VACUUM FULL

Plain `VACUUM` makes dead space reusable inside the table; it seldom shrinks the files. `VACUUM FULL` rewrites the
table, holds an exclusive lock for the whole rewrite, and needs free disk for a full copy. Decide from numbers, not
from `pg_stat_user_tables` alone:

- `n_live_tup` is reset to 0 whenever the stats are (after a crash or an immediate shutdown). A table showing 0 live
  rows and "100 % dead" may hold 55 million rows. Use `pg_class.reltuples` for the size.
- A table whose stats were reset is also skipped by autovacuum until its counters cross the threshold again, so it
  may have no planner statistics at all. One `VACUUM (ANALYZE)` fixes both.

## Measured, 2026-11-21

Root disk was at 93 %, and the question was whether the database was bloated.

- **Dead ratios:** drawers 14.5 % (258 k dead of 1.53 M rows); the graph's MENTIONS 0.3 % (168 k of 55.4 M). Neither
  was near the 30 % that would justify `VACUUM FULL`. Most of the drawers table is TOAST (4.4 GB) and indexes (6.4 GB),
  not dead rows.
- **The first run used the role's own session:** 5 of 7 VACUUMs were cancelled at 30 s. The rerun with
  `SET statement_timeout = 0` took 63 min on the drawers table and 72 s on MENTIONS, at idle I/O.
- **Result:** the database grew 8 MB; no disk came back, as expected of a plain VACUUM. The drawers table had 0 dead
  rows left, and the graph tables had planner statistics again for the first time since the stats reset.
