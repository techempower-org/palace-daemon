# mempalace_drawers (wing, id) index

A schema change on the production palace, built 2026-11-21 13:24–13:25. The fleet lead approved it at 13:16 that
day. It follows the (wing, room, id) index of 11-18 ([its run-book](mempalace-drawers-wing-room-id-index.md)), which
cannot serve the wing-only page.

## Why

mempalace pages through one wing with this query:

    SELECT id, NULL::text, wing, room, metadata FROM "mempalace_drawers"
    WHERE ("wing" = $1) AND id > $2 ORDER BY id LIMIT $3

That shape had 34 of its statements cancelled at the role's 30 s `statement_timeout` in the 48 h to 11-21 13:19. They
came in busy hours (the most, 15, during a manual VACUUM), not on every call.

Measured before the build, read-only, on four wings (the largest, and those nearest 1 %, 0.2 % and 0.05 % of all
drawers), with a page starting at each wing's 90th-percentile id: every page ran in 1–150 ms, under the custom plan
and under a forced generic plan. So the plan was not wrong. It was expensive. Without a matching index, a page:

- reads every row of the wing past `$2` through `mempalace_drawers_wing_idx`, then sorts them, for most wings;
- or walks the primary key in id order and filters by wing, for the largest wings.

Either way, a page can read many times more heap pages than the 1000 rows it returns, and on a busy disk that is what
crosses 30 s.

## Change

    CREATE INDEX CONCURRENTLY mempalace_drawers_wing_id_idx ON public.mempalace_drawers (wing, id);

Same build settings, start guard, failure handling and rehearsal as the 11-18 index, with one change: the build
gives up at 00:55, so it can never start inside 01:00–03:00 (the 02:15 nightly dump).

- The rehearsal on a throwaway postgres (the palace image) covered a build over an INVALID leftover, a rerun, and the
  rollback. It failed with the INVALID-leftover drop skipped.
- The guard cleared on the first check (load 7.4, IO full avg60 22.2 %).
- `mempalace_drawers_wing_idx` stays. Whether the new index replaces it is a separate decision.

## Rollback

    DROP INDEX CONCURRENTLY IF EXISTS public.mempalace_drawers_wing_id_idx;

Verify in a fresh session that `to_regclass('public.mempalace_drawers_wing_id_idx')` is NULL.

## Measured

Build: 1 min, 125 MB, valid and ready on the first try (checked in a fresh session).

The same four deep pages after the build, read-only `EXPLAIN (ANALYZE, BUFFERS)`. "Without" means
`enable_indexscan = off`, which approximates the old plan: it forces the bitmap + sort plan for the smaller wings. For
the largest wing the old plan was a primary-key walk, so its "without" figures approximate the bitmap plan, not that
walk. "Without" ran first and warmed the cache for "with".

| deep page (wing size) | without: buffers, time | with: buffers, time | plan with |
|---|---|---|---|
| largest wing (218,064) | 15,465, 316 ms | 1,013, 5.6 ms | Index Scan on the new index |
| ~1 % wing (15,404) | 1,083, 180 ms | 1,012, 3.4 ms | Index Scan on the new index |
| ~0.2 % wing (3,004) | 98, 456 ms | 101, 1.9 ms | still bitmap + sort (300 rows left; the planner's choice) |
| ~0.05 % wing (747) | 70, 11.6 ms | 78, 0.3 ms | Index Scan on the new index |

The first page of each wing (`id > ''`), from the 11-18 plan script: 33–57 ms before, 4.5–27 ms after, all on the new
index.

## Not verified

- The timeout rate after the build. The palace-pg-timeouts CHECK counts the wing-page kind every hour; compare a
  busy day against the 34 in 48 h above.
- A cold-cache comparison. The "with" column ran warm.
