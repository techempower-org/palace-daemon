# Claude Code Protocols

## Core Mandates

### 1. SSH-Friendly Feedback
- **Always** provide a concise, one-line terminal confirmation (e.g., '📥 Filed to {room}') after filing memories via the MemPalace MCP.
- Do not rely on desktop notifications as the user is often on SSH.

### 2. Post-Phase Documentation
- At the end of every work phase, systematically update the project's `README.md` or `CHANGELOG.md`.
- **Mandatory:** File a roadmap update via MemPalace to `wing=palace_daemon`, `room=planning` (per the canonical 7-room taxonomy — see the `palace-taxonomy` skill or `~/Projects/familiar.realm.watch/docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md`).

### 3. Service Management
- **System Service Only:** ALWAYS manage `palace-daemon` via `sudo systemctl [start|stop|restart] palace-daemon`.
- **No Manual Starts:** NEVER start the daemon manually via `python3 main.py`. Manual startup is blocked by default and requires the `--manual` flag; only use this for isolated debugging.

### 4. Memory Protocol
- **Silent Mode:** Ensure `silent_save` is enabled in MemPalace settings to prevent blocking the chat flow.
- **Roadmap Sync:** Before finishing, check `wing=palace_daemon, room=planning` to ensure the next steps are documented for the next session.
- **Wing/room layout:** Per the palace-taxonomy spec, `wing = project slug` (no `wing_` prefix), `room ∈ {architecture, decisions, problems, planning, sessions, references, discoveries}`. The session hooks already enforce this on auto-saves.

### 5. Upgrading mempalace
After `pipx upgrade mempalace`, re-apply any local patches and restart:

    bash /home/jp/Projects/palace-daemon/scripts/apply_patches.sh
    sudo systemctl restart palace-daemon

If a patch conflicts, the script will say so. Check whether upstream fixed the issue — if so, delete the patch file. Otherwise update the patch to match the new code.

Patches live in `patches/`. No active patches as of 2026-05-23 — the last patch (`mcp_server_get_collection.patch`) was absorbed into mempalace 3.3.5's `_get_collection_chroma` backend.

## Code Conventions

### Silent exception handling (palace-daemon#169)

**Background:** On 2026-05-28 a cascade of six bugs (#150, #157, #160, …) were each hidden behind silent `try/except: pass`/`continue`/`return None` handlers. Fixing one revealed the next, recursively. The thread: silent exception handling at one layer hides bugs at the next layer down.

**Convention:** When writing or reviewing a `try/except`, ask "does this except clause touch real state?"

| Pattern | Verdict |
|---|---|
| Cleanup in `finally` (`close`, `rename`, `rmdir`) | ✅ silent OK |
| Type-safety guard (e.g. `Path(s).suffix` where `s` might be non-string) | ✅ silent OK |
| Diagnostic / canary / best-effort notification (sd_notify, desktop toast) | ✅ silent OK |
| Parse user input → 4xx response | ✅ "silent" (the response IS the report) |
| Already logs via `_log.exception` / `_log.warning` | ✅ silent OK |
| **Touches real state, config, DB, or external systems** | ❌ **add `logging.warning(f"X failed: {e}")` before the recovery** |

Same recovery either way — but `logging.warning` makes the cause visible in journalctl. Pattern:

```python
# Don't:
try:
    rows = kg._run_cypher(...)
except Exception:
    continue  # silent, hides bugs for weeks

# Do:
try:
    rows = kg._run_cypher(...)
except Exception as e:
    logging.warning("op X failed for %r: %s", input, e)
    continue  # same recovery, operators can triage
```

Full decision tree + the bug cascade: palace-daemon#169.

### Library-version awareness (psycopg2 vs psycopg v3)

The daemon's direct postgres connects use `psycopg2`. `mempalace.knowledge_graph_age` uses `psycopg` (v3) internally for Cypher execution.

In code paths that go through mempalace's AGE helper, build per-error tuples that union both library variants:

```python
import psycopg2, psycopg2.errors
try:
    import psycopg, psycopg.errors as _pg_err
except ImportError:
    psycopg = None
    _pg_err = None

_oom_excs = (psycopg2.errors.OutOfMemory,)
if _pg_err is not None:
    _oom_excs = _oom_excs + (_pg_err.OutOfMemory,)
```

**Always validate with a live curl probe after deploy** — tests can pass against the wrong library hierarchy and you'd never know. PR #163 was the canonical case: tests passed against psycopg2, live behavior was unchanged because the actual errors were psycopg v3. See `tests/test_cypher_read_only.py::TestCypherStructuredErrorsPsycopg3` for the test pattern.

### `#101` main.py decomposition patterns

When extracting code from `main.py`:

- **Pure-logic helpers:** extract + re-export under `_`-prefixed name. Tests using `patch.object(main, ...)` keep working unchanged.
- **Helpers with module-state callbacks:** function-local `import main` for lazy lookup (see `daemon_tools.invalidate_rooms_cache`, `fast_intercept.fast_mcp_status_payload`).
- **Helpers with test-patched constants:** one explicit test update per constant (see `auth.PALACE_VIZ_SESSION_TTL_SECONDS`).
- **Mutable module state:** test sites that mutate it need updating to mutate the new module (see `rooms._canonical_rooms_cache`, mechanical sed across ~12 sites).
- **FastAPI route handlers:** deferred — would need APIRouter rewiring + decorator hoisting.

Cumulative: as of 2026-05-28, twelve slices done, main.py from 4751 → 3384 lines. Status doc: palace-daemon#135.
