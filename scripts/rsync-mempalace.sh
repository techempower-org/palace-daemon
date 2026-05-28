#!/usr/bin/env bash
# rsync-mempalace.sh — backup deploy path for mempalace when Syncthing fails.
#
# Mirrors scripts/deploy.sh's shape (env config + step counter + /health poll),
# but pushes the *mempalace* source tree from this workstation to the deploy
# host via rsync over SSH. Today (2026-05-28) Syncthing on familiar
# clean-exited at 07:55 PDT and didn't auto-restart; 1.5h of mempalace work
# (#288–#295, including the #292 ~100× speedup) sat undeployed because the
# daemon's editable pip install points at the on-host mempalace tree which
# was no longer being synced. This script is the manual backup for that case.
#
# Configuration (env var → default):
#   PALACE_HOST              ssh target running the daemon         (required)
#   PALACE_DAEMON_URL        base URL to poll for /health          (http://${PALACE_HOST}:8085)
#   PALACE_API_KEY           x-api-key for verify (read-only)      (empty → no auth header)
#   PALACE_SSH_USER          ssh user prefix                       (none — uses your ssh config)
#   MEMPALACE_LOCAL_DIR      mempalace source on THIS workstation  ($HOME/Projects/memorypalace)
#   MEMPALACE_REMOTE_DIR     mempalace path on the deploy host     (mirrors local path)
#   PALACE_RESTART_CMD       command to restart the daemon         (sudo systemctl restart palace-daemon)
#   PALACE_HEALTH_TIMEOUT    seconds to wait for /health           (30)
#   PALACE_VERIFY            run verify-routes after restart (1/0) (1)
#
# A site-local config file may set any of the above without exporting them
# into your shell. Searched in order, first found wins:
#   $PALACE_DEPLOY_CONF (if set), ./scripts/deploy.conf,
#   $XDG_CONFIG_HOME/palace-daemon/deploy.conf, ~/.config/palace-daemon/deploy.conf
#
# Usage:
#   scripts/rsync-mempalace.sh                       # uses config / env defaults
#   PALACE_HOST=myhost scripts/rsync-mempalace.sh
#   MEMPALACE_LOCAL_DIR=~/work/memorypalace scripts/rsync-mempalace.sh

# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

# ---------------------------------------------------------------- config file
# Same lookup order as deploy.sh — the two scripts share config knobs.
_load_conf() {
    local candidates=(
        "${PALACE_DEPLOY_CONF:-}"
        "$SCRIPT_DIR/deploy.conf"
        "${XDG_CONFIG_HOME:-$HOME/.config}/palace-daemon/deploy.conf"
        "$HOME/.config/palace-daemon/deploy.conf"
    )
    local f
    for f in "${candidates[@]}"; do
        [ -n "$f" ] || continue
        if [ -r "$f" ]; then
            # shellcheck disable=SC1090
            . "$f"
            CONF_LOADED="$f"
            return 0
        fi
    done
    return 0
}
CONF_LOADED=""
_load_conf

# ---------------------------------------------------------------- parameters
HOST="${PALACE_HOST:-}"
SSH_USER="${PALACE_SSH_USER:-}"
SSH_TARGET="${SSH_USER:+$SSH_USER@}${HOST}"

if [ -n "${PALACE_DAEMON_URL:-}" ]; then
    URL="$PALACE_DAEMON_URL"
elif [ -n "$HOST" ]; then
    URL="http://${HOST}:8085"
else
    URL=""
fi

KEY="${PALACE_API_KEY:-}"
HEALTH_TIMEOUT="${PALACE_HEALTH_TIMEOUT:-30}"
RESTART_CMD="${PALACE_RESTART_CMD:-sudo systemctl restart palace-daemon}"
RUN_VERIFY="${PALACE_VERIFY:-1}"

LOCAL_DIR="${MEMPALACE_LOCAL_DIR:-$HOME/Projects/memorypalace}"
REMOTE_DIR="${MEMPALACE_REMOTE_DIR:-$LOCAL_DIR}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ -n "$HOST" ] || fail "no deploy host — set PALACE_HOST (or a deploy.conf)"
[ -n "$URL" ] || fail "no daemon URL — set PALACE_DAEMON_URL or PALACE_HOST"
[ -d "$LOCAL_DIR" ] || fail "MEMPALACE_LOCAL_DIR not a directory: $LOCAL_DIR"
[ -n "$CONF_LOADED" ] && warn "loaded config: $CONF_LOADED"

TOTAL=3
[ "$RUN_VERIFY" = "1" ] && TOTAL=$((TOTAL + 1))
n=0
nstep() { n=$((n + 1)); step "$n/$TOTAL  $1"; }

# ---------------------------------------------------------------- 1: rsync
nstep "rsync $LOCAL_DIR/ → $SSH_TARGET:$REMOTE_DIR/"
# --delete       prune files removed upstream so we match exactly
# --exclude .git keep this script idempotent even if the local dir is a git
#                checkout (we don't want to drag .git onto the host)
# --exclude __pycache__/ and *.pyc — bytecode regenerates from .py
# --exclude .venv/ — host has its own venv via daemon's editable install
# -z, -P         compress + show progress; the tree is ~10MB so this is
#                fast enough not to need a quiet mode for non-interactive use
if ! rsync -az --delete \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='.pytest_cache/' \
    --exclude='*.egg-info/' \
    --exclude='.coverage' \
    "$LOCAL_DIR/" "$SSH_TARGET:$REMOTE_DIR/"; then
    fail "rsync failed"
fi
ok "tree synced"

# ---------------------------------------------------------------- 2: restart
nstep "restart daemon on $HOST"
ssh "$SSH_TARGET" "$RESTART_CMD" || fail "restart failed"
ok "restart issued"

# ---------------------------------------------------------------- 3: health
nstep "wait for daemon health"
deadline=$((SECONDS + HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
    if curl -fs --max-time 3 "$URL/health" >/dev/null 2>&1; then
        version=$(curl -s "$URL/health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version","?"))' 2>/dev/null || echo "?")
        ok "healthy on v$version (after $((SECONDS - (deadline - HEALTH_TIMEOUT)))s)"
        break
    fi
    sleep 1
done
(( SECONDS >= deadline )) && fail "daemon did not respond on $URL within ${HEALTH_TIMEOUT}s"

# ---------------------------------------------------------------- 4: verify
if [ "$RUN_VERIFY" = "1" ]; then
    nstep "smoke-test routes"
    PALACE_DAEMON_URL="$URL" PALACE_API_KEY="$KEY" \
        bash "$SCRIPT_DIR/verify-routes.sh" \
        || fail "verify-routes reported failures (see output above)"
fi

printf '\n\033[1;32m✦ mempalace rsync deploy complete: %s on %s\033[0m\n' "$LOCAL_DIR" "$URL"
