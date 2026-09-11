# Upstream sync assessment — 2026-07-04

**Verdict: do NOT `git merge upstream/main`.** The fork is ahead of and diverged
from `rboarescu/palace-daemon`; a branch merge would regress fork features and
inject ChromaDB-only machinery onto the postgres-primary default path.

## State at assessment

- merge-base `HEAD..upstream/main` = `d0aabb9` (**pre-v1.5.1**).
- `upstream/main` = 40 commits past base (v1.5.1 → v1.8.1).
- fork = 265 commits past base; `VERSION` was `1.9.1` (now `1.8.1+te.1` — see
  [`VERSIONING.md`](VERSIONING.md)).
- A trial merge (`git merge --no-commit --no-ff upstream/main`) = **13
  conflicted files / 101 hunks** (`main.py` 32, `monitor.py` 14,
  `static/viz.html` 13, `tests/test_mine_backend_aware.py` 9, …).

## Why not merge

Upstream's v1.7.5→v1.8.1 "features" originated in *this* fork:

| upstream commit | feature | origin |
|---|---|---|
| `8b59be2` | crash-loop config / auto-recovery / desktop-notify | our PR #24 (closes #21) |
| `8b7cb3c` | verified backups (`integrity_check` + smoke retrieval) | our PR #23 |
| `3e81de3` | `/mine` backend-aware guard | our PR #30 (fixes #29) |

The fork already carries all of them, often richer (e.g. cookie-session `/viz`
auth vs upstream's query-param `key`). Merging re-imports our own work through
upstream's simpler lens and would regress those.

## The one genuinely-upstream change

- `183fd4d` `fix(hnsw)`: `_hnsw_mtime_refresh_loop` — a workaround for
  chromadb-1.5.x false-positive HNSW quarantine (touches `data_level0.bin`
  every 60s to keep the sqlite/HNSW mtime gap under the 300s quarantine gate).
- **Applicability:** the fork's production backend is postgres
  (`MEMPALACE_BACKEND=postgres`), where this is a no-op — the HNSW quarantine
  path never runs. It only matters for the optional chroma backend. If we ever
  run chroma on chromadb 1.5.x, port it as a **backend-guarded** background task
  (skip unless `MEMPALACE_BACKEND == "chroma"`). Low priority; not worth a
  branch merge on its own.

## If a future upstream release has something we want

Cherry-pick or reimplement the specific commit onto the postgres path — do not
merge the branch. Re-run this assessment to see the current conflict shape:

```bash
git fetch upstream
git merge --no-commit --no-ff upstream/main   # inspect
git merge --abort                             # then back out
```
