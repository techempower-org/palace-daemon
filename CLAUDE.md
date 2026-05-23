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
