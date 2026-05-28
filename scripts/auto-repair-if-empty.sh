#!/usr/bin/env bash
# auto-repair-if-empty.sh — palace-daemon startup self-heal
#
# After the daemon binds, probes /search for the "vector ranked 0 — rest only
# reachable by keyword match" warning the daemon emits when the HNSW index is
# empty/quarantined despite a populated store. If detected, fires
# /repair {mode:"rebuild"} asynchronously so the daemon self-heals without
# operator intervention.
#
# Idempotent — if HNSW is healthy, this is a no-op (one /search probe).
# Safe to run on every restart.
#
# Wiring it in (any process supervisor works — the script is host-agnostic):
#   systemd:  add to the palace-daemon unit's [Service] section as
#               ExecStartPost=-/path/to/auto-repair-if-empty.sh
#             ExecStartPre would block startup and ExecStart= is the daemon
#             itself; Post means the daemon is up and can answer its own
#             /search before this runs. The leading "-" keeps a probe failure
#             from faulting the unit.
#   manual:   run it after starting the daemon by hand.
#
# Configuration (env var → default):
#   PALACE_PORT                  daemon port                 (8085)
#   PALACE_ENV_FILE              file sourced for the API key (~/.config/palace-daemon/env)
#   PALACE_API_KEY               x-api-key (or via env file)  (empty → no auth)
#   PALACE_AUTO_REPAIR_WAIT_SECS seconds to wait for /health  (240)
#       Large palaces (100K+ drawers) take 1-2 min to load HNSW segments from
#       disk on first start; 240s gives runway. Smaller palaces can drop this
#       to ~30. It never hangs forever — past the deadline it exits cleanly.

set -uo pipefail

PORT="${PALACE_PORT:-8085}"
HOST="127.0.0.1"
ENV_FILE="${PALACE_ENV_FILE:-$HOME/.config/palace-daemon/env}"

log() {
  echo "[auto-repair] $*"
  command -v systemd-cat >/dev/null 2>&1 \
    && echo "[auto-repair] $*" | systemd-cat -t palace-daemon-auto-repair -p info
}

# Source the env file so PALACE_API_KEY is available (matching the daemon's own env)
if [ -r "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
KEY="${PALACE_API_KEY:-}"
HEADERS=()
[ -n "$KEY" ] && HEADERS=(-H "x-api-key: $KEY")

# Ensure the canonical-mapping .pth file is in place before any downstream
# import touches kg_canonical_writepass (issue #79). Idempotent — no-op if
# the .pth is already correct. Failure here doesn't block self-heal; the
# script logs and continues.
_pth_installer="$(dirname "$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || echo "$0")")/install-canonical-pth.sh"
if [ -x "$_pth_installer" ]; then
  "$_pth_installer" 2>&1 | while IFS= read -r line; do log "$line"; done || \
    log "install-canonical-pth.sh exited non-zero (non-fatal)"
fi

# Wait up to WAIT_SECS for the daemon to start responding to /health.
WAIT_SECS="${PALACE_AUTO_REPAIR_WAIT_SECS:-240}"
log "waiting up to ${WAIT_SECS}s for daemon on ${HOST}:${PORT}..."
for i in $(seq 1 "$WAIT_SECS"); do
  if curl -sS --max-time 2 "${HEADERS[@]}" "http://${HOST}:${PORT}/health" >/dev/null 2>&1; then
    log "daemon up after ${i}s"
    break
  fi
  sleep 1
  if [ "$i" = "$WAIT_SECS" ]; then
    log "daemon never came up after ${WAIT_SECS}s — bailing"
    exit 0  # don't fail the unit; just don't auto-repair
  fi
done

# Probe /search for the "vector ranked 0" warning.
# /search only accepts q, limit, wing, room (main.py:999-1006) — a previous
# revision passed kind=content but FastAPI silently dropped it. Drop the
# unsupported param so the URL matches what the endpoint actually parses.
PROBE=$(curl -sS --max-time 10 "${HEADERS[@]}" \
  "http://${HOST}:${PORT}/search?q=auto-repair-probe&limit=1" 2>/dev/null)
if [ -z "$PROBE" ]; then
  log "search probe returned nothing — bailing"
  exit 0
fi

# jq is small enough to assume; fall back to grep if missing
if command -v jq >/dev/null 2>&1; then
  WARN=$(echo "$PROBE" | jq -r '.warnings[]? // empty' 2>/dev/null)
else
  WARN=$(echo "$PROBE" | grep -oE 'vector ranked [0-9]+' || true)
fi

if echo "$WARN" | grep -qE 'vector ranked 0|vector ranked 1[^0-9]'; then
  log "DETECTED degraded HNSW recall: $(echo "$WARN" | head -1)"
  log "kicking off /repair {mode:\"rebuild\"} in background — daemon stays available"
  # Fire-and-forget; the daemon's /repair endpoint serializes on the rebuild
  # semaphore, so concurrent silent-saves auto-queue to pending.jsonl during.
  # mktemp avoids the symlink-attack risk of a predictable /tmp filename.
  repair_log="$(mktemp "${TMPDIR:-/tmp}/palace-auto-repair-XXXXXX.log" 2>/dev/null || echo "/tmp/palace-auto-repair-$$.log")"
  nohup curl -sS --max-time 7200 -X POST \
    "${HEADERS[@]}" \
    -H 'content-type: application/json' \
    "http://${HOST}:${PORT}/repair" -d '{"mode":"rebuild"}' \
    > "$repair_log" 2>&1 &
  log "auto-rebuild PID=$! — log $repair_log"
else
  log "HNSW recall looks healthy (no 'vector ranked 0' warning)"
fi

exit 0
