# mempalace-db

Postgres 16 + pgvector + Apache AGE container that backs palace-daemon.

This directory codifies the container that previously lived as an ad-hoc
`docker run` invocation on the familiar host (migrated from disks 2026-05-24,
see `project_palace_stack_migration_familiar.md` in the operator's notes).
Codifying it here puts the cgroup memory ceiling and postgres tuning under
version control so future operators don't have to spelunk `docker inspect`
to find out why postgres OOMed.

## Layout

| File | Role |
| --- | --- |
| `Dockerfile` | apache/age:release_PG16_1.6.0 + postgresql-16-pgvector |
| `init.sql` | First-boot `CREATE EXTENSION vector; CREATE EXTENSION age;` |
| `postgresql.conf` | Tuned config sized for a 6 GiB cgroup on the 15 GiB familiar host |
| `docker-compose.yml` | Service definition with `mem_limit: 6g`, healthcheck, bind mounts |

## Applying for the first time on a fresh host

```bash
export MEMPALACE_DB_PASSWORD=$(bw get password mempalace-db)
docker compose -f mempalace-db/docker-compose.yml up -d
```

## Applying to an existing live container (zero-downtime memory bump)

The container on familiar today is not yet managed by this compose file; it
was created via `docker run` on 2026-05-24. To raise the cgroup ceiling
without restarting (preserving connections and the buffer pool):

```bash
ssh familiar 'docker update --memory=6g --memory-swap=6g mempalace-db'
ssh familiar 'docker stats --no-stream mempalace-db'   # confirm new limit
```

The postgresql.conf tuning (shared_buffers 4GB → 2GB → 3GB, max_connections
200 → 32, effective_cache_size 12GB → 6GB) requires a postgres restart to
take effect. Schedule alongside a maintenance window — see palace-daemon#102
and familiar.realm.watch#61 for the full sequence.

## Backup before any destructive op

```bash
ssh familiar 'sudo /usr/local/bin/mempalace-backup.sh'
```

## Why these numbers

See the comment block at the top of `docker-compose.yml` and the inline
notes in `postgresql.conf`. Short version:

- 3 GiB ceiling + 4 GiB declared shared_buffers was mathematically
  impossible — postgres OOMed every time it tried to page in its own
  shared region (5+ kills in 1h on 2026-05-28).
- 6 GiB ceiling + 3 GiB shared_buffers gives postgres ~4.5 GiB of working
  set with ~20% headroom, while leaving ~7 GiB on the host for llama-server
  (3.1 GiB), palace-daemon (~1 GiB), familiar-api, kg-extract, and the OS.
  The bump from 2 GiB → 3 GiB was unlocked by dropping max_connections
  from 200 → 32 (observed peak: ~16 backends), which cut worst-case
  work_mem reservation from 6.4 GiB to 1 GiB.
- effective_cache_size lowered from 12 GiB to 6 GiB to match what the host
  can actually keep hot — the planner was previously being told to assume
  a page cache that never existed.

Refs: techempower-org/familiar.realm.watch#50,
techempower-org/palace-daemon#102.
