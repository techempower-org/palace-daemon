#!/usr/bin/env bash
# auto-repair-if-empty.sh — palace-daemon startup self-heal
#
# Runs as ExecStartPost on the palace-daemon systemd unit. After the daemon
# binds, probes /search for the "vector ranked 0 — rest only reachable by
# keyword match" warning that the jphein-fork emits when the HNSW index is
# empty/quarantined despite a populated SQLite. If detected, fires
# /repair {mode:"rebuild"} asynchronously so the daemon self-heals without
# operator intervention.
#
# Idempotent — if HNSW is healthy, this is a no-op (one /search probe).
# Safe to run on every restart.
#
# Why ExecStartPost: ExecStartPre would block daemon startup; ExecStart=
# already the daemon itself. Post means daemon is up and responding before
# this runs, so we can probe its own /search.

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

# Wait up to WAIT_SECS for the daemon to start responding to /health.
# 151K-drawer palaces take ~60-120s to load HNSW segments from disk on
# first startup; 30s wasn't enough and the script bailed exactly when
# auto-repair was most useful. 240s gives generous runway without making
# the unit hang forever if the daemon is genuinely broken.
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
  # semaphore, so familiar's silent-saves auto-queue to pending.jsonl during.
  nohup curl -sS --max-time 7200 -X POST \
    "${HEADERS[@]}" \
    -H 'content-type: application/json' \
    "http://${HOST}:${PORT}/repair" -d '{"mode":"rebuild"}' \
    > /tmp/palace-auto-repair-$(date +%s).log 2>&1 &
  log "auto-rebuild PID=$! — log /tmp/palace-auto-repair-*.log"
else
  log "HNSW recall looks healthy (no 'vector ranked 0' warning)"
fi

exit 0
