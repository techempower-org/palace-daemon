# mempalace-db: image 0.2 (PG 16.15) and a loopback-only port (2026-10-21)

## What changed
- Image `mempalace-db:0.1` → `mempalace-db:0.2` (#310). It's the same `apache/age:release_PG16_1.6.0` base plus
  `apt-get upgrade` at build time:
  - PostgreSQL 16.10 → **16.15**;
  - OpenSSL 3.5.1 → 3.5.7;
  - pgvector 0.8.2 → **0.8.7** (`ALTER EXTENSION vector UPDATE` was run);
  - AGE 1.6.0, unchanged.
- The port moved from `"5433:5432"` (published on 0.0.0.0 and [::]) to **`127.0.0.1:5433` and `[::1]:5433` only**.

## Why the port
Docker's port publishing writes its own iptables DNAT rules ahead of ufw, so `ufw` never saw 5433: postgres was
reachable from the whole LAN (password auth only). `pg_stat_activity.client_addr` can't show who connects, because
every TCP client appears as the bridge gateway (172.18.0.1). So clients were inventoried with conntrack:
- `sudo conntrack -L -p tcp --orig-port-dst 5433` was sampled every 30 s from 16:21 to 22:53, about 6.5 h.
- Non-loopback sources seen: **only the positive-control test connection** (a bare TCP connect from another LAN host).
- A second control at 22:53 was also seen, so the instrument was live at both ends.
- All real clients (palace-daemon, the mempalace CLI, operator tooling) use `localhost:5433` (`~/.mempalace/dsn.env`).

**To allow a remote host:** add one specific bind to `ports:` (for example its Tailscale IP, `"100.x.y.z:5433:5432"`).
Don't go back to an unqualified `"5433:5432"`.

## How the swap ran (23:06, one recreate)
1. Paused mining with the bench lock: `/srv/mempalace-data/palace/.bench-active.lock`, the daemon's
   `MEMPALACE_PALACE_PATH`. Waited until no `mempalace mine` was running.
2. Fresh dump: `systemctl start mempalace-backup` → `/srv/backups/databases/mempalace-20261021.sql.gz`, 3.49 GB.
   `gzip -t` ok, and it ends with "PostgreSQL database dump complete".
3. `docker compose up -d --no-build mempalace-db`: healthy after 66 s.
   (`POSTGRES_PASSWORD` is empty in the compose env, as it was before. It's read only at first init.)
4. `ALTER EXTENSION vector UPDATE;`, then the checks below.
5. Removed the bench lock.

| check | before | after |
|---|---|---|
| `select version()` | 16.10 | **16.15** |
| age / vector | 1.6.0 / 0.8.2 | 1.6.0 / **0.8.7** |
| drawers | 1,211,378 | 1,211,412 (hook saves continued) |
| AGE `mempalace_kg` | — | MATCH (n) count = 2,869,812; edges traverse |
| daemon `/health` | ok | ok, db_errors 0 |
| connect from another LAN host | connected | **refused** |
| connect 127.0.0.1 / ::1 | ok | ok / ok |

## Rollback
Set `image: mempalace-db:0.1` and/or `"5433:5432"` in `mempalace-db/docker-compose.yml`, then
`docker compose up -d --no-build mempalace-db`. A 16.x minor downgrade is on-disk compatible. The 0.8.2 pgvector
library loads SQL objects updated to 0.8.7 within 0.8.x, though not tested here. The dump above is the last resort.
