# Design: backend-aware `/mine` guard (concurrent ChromaDB client)

**Issue:** [rboarescu/palace-daemon#29](https://github.com/rboarescu/palace-daemon/issues/29)
**Date:** 2026-05-27
**Status:** approved (design)

## Problem

`POST /mine` spawns `mempalace mine` as a subprocess (`main.py:2797-2810`). That
subprocess opens its **own** ChromaDB `PersistentClient` on the palace path while
the daemon already holds one. ChromaDB 1.x's Rust backend cannot tolerate two
`PersistentClient` instances (in- or cross-process) on the same path — the log
store corrupts:

```
chromadb.errors.InternalError: ... Failed to pull logs from the log store
```

Mining then *appears* to succeed (200 OK, CPU spike) but persists **zero**
drawers, and recovery requires `mempalace repair`. The daemon's `_mine_sem` only
serializes daemon-mediated work; it cannot constrain the subprocess's independent
handle.

### Why this does not affect our deployment

`familiar` runs `MEMPALACE_BACKEND=postgres` (pgvector + Apache AGE). Postgres
handles concurrent connections natively, so the dual-client corruption cannot
occur there. This fix is therefore a **proper upstream fix for chroma users**
(issue filed by the upstream maintainer), landed fork-side first as
defense-in-depth in case a chroma fallback/transitional state ever recurs.

## Approach

Make `/mine` **backend-aware**:

- **postgres** → unchanged. Subprocess under `_mine_sem`, exactly as today.
- **chroma** → guarded *lock-and-reopen* choreography that guarantees only one
  `PersistentClient` touches the files at any instant.

This is the issue's "alternative" fix. The issue's "preferred" fix (route mine
writes through the daemon API / mine in-process) is deferred because it requires
`mempalace` library changes — captured as
[techempower-org/mempalace#261](https://github.com/techempower-org/mempalace/issues/261).
When #261 lands, the daemon can drop the subprocess entirely and mine through its
single client; this design is the correct interim fix and remains valid as a
fallback.

## Chroma choreography

Inside `/mine`, when `getattr(_mp._config, "backend", "chroma") == "chroma"`:

1. **Enter `_exclusive_palace()`** (`main.py:489`) — acquire every read/write/mine
   slot so no daemon-mediated work runs during the mine. Reuses the primitive
   built for `/repair` rebuild.
2. **Deterministically release the daemon's client.** Drop the mcp-local caches
   *and* close the pooled backend client so chromadb's Rust-side SQLite file lock
   is actually released:
   ```python
   _mp._collection_cache = None
   _mp._client_cache = None
   from mempalace.palace import get_backend
   get_backend("chroma").close_palace(_mp._config.palace_path)
   ```
   **Critical:** this must call `close_palace()` (→ `_close_client()` →
   `client.close()`), **not** `_force_chroma_cache_reset()`, which only `.pop()`s
   the `_clients` dict and leaks the lock until GC (see
   [mempalace#262](https://github.com/techempower-org/mempalace/issues/262)). A
   bare pop would leave the daemon's old handle holding the lock when the
   subprocess opens it — reproducing the very corruption we are preventing.
3. **Spawn the subprocess** — now the sole client — and `await proc.communicate()`.
4. **Reopen** the daemon's client via `_mp._get_collection(True)` in an executor
   (the warmup pattern at `main.py:909`).

Steps 2 and 4 are wrapped so reopen always runs (see Error handling).

### On the flush sleep

The existing shutdown teardown (`main.py:1010`) drops refs + `gc.collect()` +
`sleep(~2s)` because it predates a clean close. Since `close_palace()` calls the
real `PersistentClient.close()` and releases the Rust lock **synchronously**, the
sleep is **not required** for correctness here. We keep an optional
`PALACE_CHROMA_FLUSH_SECONDS` (default `0.0`) as a tunable safety margin for very
large palaces, but the deterministic close is the mechanism, not the sleep.

## Targeted cleanup (in scope)

The client-teardown sequence is currently duplicated (shutdown `:1022`, auto-repair
`:479`). Since we are adding a third caller, factor a single helper:

```python
def _drop_chroma_client(close: bool = True) -> None:
    """Drop daemon chroma client caches; optionally release the Rust file lock.

    close=True calls backend.close_palace() for a deterministic lock release
    (required before handing the palace to an external writer). close=False
    keeps the legacy cache-only drop for shutdown, where the process is exiting.
    """
```

Shutdown and auto-repair keep their current behavior via the flag; the new mine
path passes `close=True`. One implementation of the corruption-sensitive
sequence instead of three.

## Error handling

- The release/reopen pair runs in a `try/finally` **inside** `_exclusive_palace()`:
  reopen happens even if the subprocess exits non-zero or raises.
- If `_get_collection(True)` itself throws on reopen, the caches remain `None`, so
  the **next** request lazily reopens — self-healing. Log `CRITICAL` regardless.
- The existing repair-in-progress queue path (`_enqueue_pending_mine`) stays in
  front, unchanged: a mine arriving during `/repair` rebuild is still queued and
  drained, never executed against a mid-swap collection.

## Data flow

Drawer content and the `mempalace mine` invocation are unchanged. Only the
*concurrency choreography around* the subprocess changes, and only on chroma.

## Configuration

| Env | Default | Purpose |
|-----|---------|---------|
| `PALACE_CHROMA_FLUSH_SECONDS` | `0.0` | Optional post-close settle margin for very large palaces. `0` relies on the synchronous `close_palace()`. |

## Validation (throwaway chroma palace)

1. **Repro the bug → confirm fix.** Create a temp palace with
   `MEMPALACE_BACKEND=chroma`. Start the daemon in `--manual` mode (the sanctioned
   isolated-debug start) against it. Fire the issue's `POST /mine` on a sample
   convo export. Assert: (a) drawer count increases by the expected amount and
   (b) a subsequent `/search` succeeds (collection readable — no log-store
   corruption). Without the fix, (a) is 0 and (b) eventually errors.
2. **Postgres regression.** Confirm the postgres branch still spawns the
   subprocess with no `_exclusive_palace`/close added (code path assertion +
   smoke mine against the dev postgres palace if available).

## Validation outcome (2026-05-27)

Validated against throwaway chroma palaces with the real `mempalace mine`
subprocess (driven via `mempalace --palace <p> mine` to force the in-process
local path, bypassing daemon routing).

- **Fix path — PASS.** Hold a daemon-style client → `close_palace()` → run the
  mine subprocess → reopen via `get_collection` → read. Drawers persisted
  (baseline 2 → 4) and the collection remained readable. The choreography is
  correct and non-destructive.
- **Bug repro — not reproduced on chromadb 1.5.8.** Holding the daemon client
  open *across* the mine (no close) still persisted drawers and read cleanly.
  A direct two-`PersistentClient`-same-path test with interleaved writes also
  did **not** corrupt under 1.5.8. So #29's log-store corruption is specific to
  the chroma 1.x version the upstream maintainer hit; 1.5.8 (this fork's pin)
  tolerates the dual-client window.
- **Postgres regression — covered by unit test** (`test_mine_backend_aware.py::
  test_postgres_keeps_lightweight_path`): the postgres branch does not enter
  `_exclusive_palace()` and does not close the client.

This reframes the fork-side fix as **correct defense-in-depth** (harmless, and
right for the chroma versions where the bug bites) rather than a fix for a
locally-live bug. Our deployment is postgres and was never susceptible. The
chroma-version characterization is reported back on upstream #29.

## Risks

- **Read/write-blind window** during the mine: identical to what `/repair` rebuild
  already imposes, and bounded by mine duration. No `/health` "mining" surface —
  YAGNI.
- **Reopen failure** leaves the daemon temporarily clientless; mitigated by the
  lazy-reopen self-heal and a CRITICAL log.

## Relationship to filed mempalace issues

- **#261** (injectable backend for `miner.mine()`) — the substrate change that
  would replace this subprocess dance with true single-client in-process mining.
  This design is the correct fix until then.
- **#262** (cache reset leaks the Rust lock) — this design *depends on* using
  `close_palace()` rather than the leaky `_force_chroma_cache_reset()`. If #262 is
  fixed mempalace-side, the daemon can call the reset helper instead; until then
  the daemon calls `close_palace()` directly.
