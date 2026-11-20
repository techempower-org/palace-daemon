# mempalace_drawers (wing, room, id) index

A schema change on the production palace, built 2026-11-18 23:05. The fleet lead approved it at 15:24 that day,
after JP asked "why did the palace time out?".

## Why

mempalace pages through one room of one wing with this query:

    SELECT id, document, wing, room, metadata FROM "mempalace_drawers"
    WHERE (("wing" = $1) AND ("room" = $2)) ORDER BY id LIMIT $3

No index served both the filter and the order. The planner walked the primary key in id order and filtered row by
row. For a large (wing, room) whose rows are spread through the id order, that took longer than the role's 30 s
`statement_timeout`, and postgres cancelled it.

## Change

    CREATE INDEX CONCURRENTLY mempalace_drawers_wing_room_id_idx ON public.mempalace_drawers (wing, room, id);

**Build settings** (session only):
- `maintenance_work_mem = 256MB`, because the container is capped at 6 GB and `shared_buffers` is 3 GB.
- `max_parallel_maintenance_workers = 0`, so one backend did the reading.
- That backend was moved to the idle I/O class at nice 15 through its host pid. The palace host's standing rule is
  that every heavy read runs under `ionice -c3 nice -n 15`.

**Start guard:** the build started only if the 1-min load was ≤ 8 and the IO PSI "full" avg60 was ≤ 30 %. Otherwise
it retried every 10 min, giving up at 01:55, before the 02:15 nightly dump. It started on the first check (load 1.4,
IO full 3.6 %).

**Failure handling:** a build that failed, or left the index INVALID, was to be rolled back at once and verified gone
in a fresh session.

**Rehearsal:** the build and rollback scripts were tested end to end on a throwaway postgres (the palace image), with
fake rows inserted in random order like production. The test covered a build, an INVALID leftover from a failed
`CREATE INDEX CONCURRENTLY` dropped and rebuilt, an idempotent rerun, and the rollback. A perturbation (skipping the
INVALID-leftover drop) turned the test red.

## Rollback

    DROP INDEX CONCURRENTLY IF EXISTS public.mempalace_drawers_wing_room_id_idx;

The drop takes no lock that blocks reads or writes and touches no data. Verify in a fresh session that
`to_regclass('public.mempalace_drawers_wing_room_id_idx')` is NULL.

## Measured

Read-only `EXPLAIN (ANALYZE, BUFFERS)`, page size 1000, on three (wing, room) pairs picked by size.

| | before (11-18 15:25) | after (11-18 23:05, warm) | after (11-19 11:0x, cold) |
|---|---|---|---|
| wing+room page, 84,629-drawer (wing, room) | **45.4 s**, Index Scan on pkey | 4.5 ms, Index Scan on the new index | 146 ms |
| wing+room page, 1,013-drawer (wing, room) | 0.31 s | 4.0 ms | 78 ms |
| wing+room page, 201-drawer (wing, room) | 0.03 s | 0.3 ms | 8 ms |

| statement timeouts of the wing+room shape | before | after |
|---|---|---|
| per hour (palace-pg-timeouts kinds, by shape) | 73 in the 48 h before the build (1.5/h, up to 26/h counting all wing-page shapes at busy hours) | **0** in the 12 h after |

Build: 1 min, 141 MB, valid and ready on the first try.

## Not covered

Paging by wing alone (`WHERE "wing" = $1 AND id > $2 ORDER BY id`) cannot use a (wing, room, id) index. That shape
had 70 timeouts in 60 h, plus 11 for wing-only, and still had 3 in the 12 h after this build. It needs a (wing, id)
index, which could replace `mempalace_drawers_wing_idx`; that is proposed separately.
