# Session Handoff — 2026-05-28

## What landed this session

Triggered by JP's morning filing of #150 (LongMemEval reported `/search/age-fused` returning 5.5× narrower context than `/search` default; QA-acc dropped 60% → 17%).

Cascade of findings + fixes:

| # | Topic | Outcome |
|---|---|---|
| #150 | LongMemEval regression | closed via #156 + #158 |
| #156 | `vec_ranks` keyed off wrong field (`id` vs `drawer_id`) — vector half of RRF was effectively disabled | merged |
| #157 | `/search/age-fused` Cypher rejected by AGE 1.5 — silent except hid it for weeks | closed via #158 |
| #159 | added `logging.warning` to every silent except in `kg_reader` + `rooms` | merged |
| #160 | `/graph.kg_triples` + `kg_mentions` silently empty — AGE Cypher walks exhaust shared memory at 1.86M+ rows | closed via #161 (CTE-bounded direct SQL on label tables) |
| #162→#163→#164→#165 | `/cypher` returned generic 500 for all postgres errors. Fix needed 4 PRs because of psycopg2 vs psycopg3 library mismatch + test-env gap. Final: structured 400/504/507/etc with error tag + hint per failure mode. |
| #166 | `/health` degraded-state cause now logged | merged |
| #167 | `postgres.postgres_dsn` config-lookup failure now logged | merged |
| #168 | `read_kg_postgres_stats` KG init failure now logged | merged |
| #169 | filed: silent-exception decision-tree convention (informational) |

Plus earlier this session:

- **`#101` decomposition:** 12 slices, `main.py` went 4751 → **3384 lines (-29%)**. Modules created: `bench_lock`, `canaries`, `db_errors`, `postgres`, `daemon_tools`, `fast_intercept`, `kg_reader` (JP's #134), `crash_loop`, `auth`, `rebuild_progress`, `path_map`, `rooms`.
- **`#136` shutdown chain:** #137 flush timeout, #138/#139 mine subprocess tracking, #141 uvicorn graceful_shutdown bound. 30s SIGKILL escalation → 2-5s clean.
- **`#140`:** `/mcp` `tools/list` augmented with 6 daemon-native tool descriptors.
- **`#143`:** `/health` no longer false-positives 503 on `crash_loop=True`.
- **`#151`:** deploy.sh warms `/graph` before running smoke (cold-start race fix).
- **`#154`:** `OOMScoreAdjust=-500` keeps userland `earlyoom` from killing the daemon during memory-heavy `/graph` calls.
- **`v1.9.1`** tagged + deployed.

Final counts: **27 PRs merged today**, **495 → 520 tests**, **9 issues closed**, **18+ clean production deploys**.

## Production state at handoff

- Daemon: **v1.9.1**, `status=ok`, `memcg=37%`, `db_errors_300s=0`
- `/graph`: returning 1000 `kg_triples` + 1000 `kg_mentions` (was 0 since 1.8.2)
- `/search/age-fused`: graph fusion working, `n_graph` thousands for entity-rich queries (was 0 since the endpoint was added)
- `/cypher`: structured HTTP responses (400/403/504/507/502) for all psycopg{,2} error types
- `/health`: HTTP 200 with `crash_loop=True` informational fields when palace serves but recently restarted
- Shutdown: clean 2-5s every deploy, no SIGKILL escalation

## Open palace-daemon work

### Externally blocked
- **`#80`** — hybrid candidate-strategy scorer-weight tuning. Blocked on SME bench re-run. Now that `/search/age-fused` actually fuses (was just vector-only before today), the bench can produce meaningful numbers. JP needs to kick off the SME run or grant fresh sister-fork scope to do it from this repo.

### Judgment-call deferred
- **`#101`** — paused at 12 slices. Remaining candidates documented in `#135`:
  - Search route handlers (~400 lines, FastAPI decorator hoisting needed)
  - WatcherService loop (lifespan-entangled)
  - KG triple-worker subprocess management (heavy state)
  - `_fast_status_payload` + `_read_kg_postgres_stats` (~150 lines, requires test updates to patch via new module rather than `main`)
  
  None are blocking. main.py at 3384 lines is comfortable.
- **`#135`** — status document, not actionable.
- **`#169`** — convention documentation, informational.

## Conventions established this session

### Silent-exception decision tree (#169)

For every `try/except: pass`/`continue`/`return None`/`return []`:

| Pattern | Verdict |
|---|---|
| Cleanup in `finally` | ✅ silent OK |
| Type-safety guard | ✅ silent OK |
| Diagnostic / canary / best-effort notification | ✅ silent OK |
| Parse user input → 4xx response | ✅ "silent" (the response IS the report) |
| Already logs via `_log.exception` | ✅ silent OK |
| **Touches real state, config, DB, or external systems** | ❌ **add `logging.warning(...)` before the recovery** |

This isn't blocking — it's a convention for future code review. Documented in `#169` and `palace_daemon/references` drawer.

### `#101` extraction patterns

- **Pure-logic helpers:** extract + re-export under `_`-prefixed name. Tests using `patch.object(main, ...)` keep working.
- **Helpers with module-state callbacks:** function-local `import main` for lazy lookup (`daemon_tools.invalidate_rooms_cache`, `fast_intercept.fast_mcp_status_payload`).
- **Helpers with test-patched constants:** one explicit test update per constant (`auth.PALACE_VIZ_SESSION_TTL_SECONDS`).
- **Mutable module state:** test sites that mutate it directly need updating to mutate the new module (`rooms._canonical_rooms_cache`, ~12 sites mechanical sed).
- **FastAPI route handlers:** deferred — would need APIRouter rewiring.

### Library-version awareness

`psycopg2` and `psycopg` (v3) have separate exception class hierarchies. The mempalace AGE helper uses v3, the daemon's direct postgres connects use v2. When catching postgres errors:

- Build per-error tuples that union both library variants
- Tests against the wrong library can pass while production behavior is unchanged — always validate with a live curl probe after deploy

### Deploy script flow

`scripts/deploy.sh` now reliably runs end-to-end clean:
1. `systemctl restart palace-daemon` (5s clean shutdown thanks to #137/138/139/141)
2. Wait for `/health` 200 (sub-second)
3. Warm `/graph` cache (28-35s cold; not failure-fatal)
4. `verify-routes.sh` smoke (all 10 probes green thanks to #154 OOM adjust)

Total ~60s per deploy.

## What's been validated in production

- 27 deploys, all clean shutdowns post-`#137`/`#138`/`#139`/`#141`
- `/graph` returns 1000 triples + 1000 mentions for the production palace
- `/search/age-fused` returns 473-82090 chars depending on query (was uniformly 457)
- `/cypher` returns HTTP 400 with structured detail for bad queries (was generic 500)
- `/mcp` `tools/list` returns 40 tools (was 34, missing the 6 daemon-native)
- `/health` returns 200 + `crash_loop=True` for rapid-deploy windows (was 503)

## For the next session

If you're picking this up:

1. Production is healthy. No firefighting needed.
2. The remaining open issues (`#80`, `#101`, `#135`, `#169`) are deferred-with-reasons, not blocked-by-mystery.
3. If a new bug surfaces in journalctl, the silent-exception sweep means it'll appear as a `logging.warning(...)` line rather than as a silent fallback. Look in the journal first.
4. If you want to keep slicing `#101`: the next slice should be `_fast_status_payload` + `_read_kg_postgres_stats` (~150 lines). Test patches need updating from `main` to the new module — mechanical but invasive enough to slow you down.
5. If you want to revisit `#80`: needs the SME bench. Either get fresh scope to drive it from this repo, or hand off to JP to run.
