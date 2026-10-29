# Backends

What palace-daemon fronts and what it runs on. The ingest sources, and
the counts of what each has filed, live in memorypalace's
[`docs/BACKENDS.md`](https://github.com/techempower-org/mempalace/blob/main/docs/BACKENDS.md):
the daemon only receives what those adapters produce.
`~/.claude/scripts/backends-report.sh` reads both files for the daily
brief. Add a row in the same PR as the backend.

Measured on the palace host on 2026-10-27 from the running daemon (pid 115558,
`/health` version 1.10.0) and its environment.

## Ingest endpoints

| Endpoint | Tier | Fed by | Needs |
| --- | --- | --- | --- |
| `POST /mine` (+ `GET /mine/status`) | live | Stop/PreCompact hooks, `mempalace mine`, the drain queue | #249: drain in-process with a warm embedder |
| `POST /silent-save` | live | the silent hook save (`hook_silent_save: true`) | #259: `session_id` as a field |
| `POST /memory` | live | MCP and CLI writes | — |
| a call-record route for livekit-voice | missing | — | lands with memorypalace's call-record adapter |

## Storage and model backends

| Backend | Tier | Role | Needs |
| --- | --- | --- | --- |
| PostgreSQL + pgvector (`mempalace-db` container, `:5433`) | live | drawers, BM25, trigram, HNSW | #256: the daemon runs pinned at its MemoryMax |
| Apache AGE | live (store) | `/graph`, KG queries | #210: server-side aggregates |
| flashrank `ms-marco-TinyBERT-L-2-v2` | live | rerank (`PALACE_RERANK_ENABLED=true`, `PALACE_RERANK_MODEL` to override) | #276: live-rerank coverage in CI |
| all-MiniLM-L6-v2 (via mempalace) | live | query embeddings | #225: no query-embedding cache |

## Clients

| Client | Tier | Path |
| --- | --- | --- |
| MCP (search-only) | live | `clients/mempalace-mcp.py` via `mempalace-mcp-wrapper.sh` |
| HTTP API | live | `X-API-Key`, key in `~/.config/palace-daemon/env` |
| `mempal-fast.py` | not measured | stdlib-only Stop hook client (skips the mempalace import); no live caller found in `~/.claude/hooks` on 2026-10-27 |

## Backups

Measured 2026-10-27 23:2x. Each copy has its own retention; none is managed by this repo.

| Copy | Where | Schedule | Retention | Size now |
| --- | --- | --- | --- | --- |
| `pg_dump` (gzip `--rsyncable`) | the palace host's backup disk (24% used) | `mempalace-backup.timer` 02:15 (`/usr/local/bin/mempalace-backup.sh`, from the host's ops repo) | 7 days by mtime (8 dumps) | 27 GB, ~3.5 GB each |
| NFS copy of that dump | disks `/mnt/raid/backups/databases` | `database-backup.timer` 02:30 (`/usr/local/bin/backup_databases.sh`) | newest 2 by filename date (`MEMPALACE_KEEP=2`, since 2026-10-13) | 7.1 GB |
| Borg snapshot of the RAID | disks `/mnt/storage/backups/borg-raid` (not on `/mnt/raid`) | 03:00 | 7 daily, 4 weekly, 6 monthly | not measured |

The RAID copy is capped by count, so mempalace adds a fixed ~7 GB to `/mnt/raid`, not a growing share. Raising the
cap would add ~3.5 GB per dump kept. Older restore points live in Borg and, for 7 days, on the palace host.
