#!/usr/bin/env bash
# rsync-palace-daemon.sh — backup deploy path for palace-daemon when Syncthing fails.
#
# Companion to scripts/rsync-mempalace.sh. scripts/deploy.sh is the primary
# deploy path (git push + ssh restart, with Syncthing carrying the code to
# the deploy host). When Syncthing is dead or out of sync, this script
# rsyncs the palace-daemon source tree directly and restarts the daemon.
#
# Today (2026-05-28 v1.9.0 deploy, issue #114) Syncthing on familiar was
# idle but had a 6-day-old palace-daemon snapshot; deploy.sh restarted
# successfully but loaded the old VERSION constant. This script unblocks
# that case.
#
# Configuration (env var → default):
#   PALACE_HOST              ssh target running the daemon         (required)
#   PALACE_DAEMON_URL        base URL to poll for /health          (http://${PALACE_HOST}:8085)
#   PALACE_API_KEY           x-api-key for verify (read-only)      (empty → no auth header)
#   PALACE_SSH_USER          ssh user prefix                       (none — uses your ssh config)
#   PALACE_LOCAL_DIR         palace-daemon source on THIS host     (auto: git rev-parse --show-toplevel)
#   PALACE_REMOTE_DIR        palace-daemon path on the deploy host (mirrors local path)
#   PALACE_RESTART_CMD       command to restart the daemon         (sudo systemctl restart palace-daemon)
#   PALACE_HEALTH_TIMEOUT    seconds to wait for /health           (30)
#   PALACE_VERIFY            run verify-routes after restart (1/0) (1)
#
# A site-local config file may set any of the above without exporting them
# into your shell. Same lookup order as deploy.sh / rsync-mempalace.sh:
#   $PALACE_DEPLOY_CONF (if set), ./scripts/deploy.conf,
#   $XDG_CONFIG_HOME/palace-daemon/deploy.conf, ~/.config/palace-daemon/deploy.conf
#
# Usage:
#   scripts/rsync-palace-daemon.sh                       # uses config / env defaults
#   PALACE_HOST=myhost scripts/rsync-palace-daemon.sh

# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

# ---------------------------------------------------------------- config file
_load_conf() {
    if [ -n "${PALACE_DEPLOY_CONF:-}" ]; then
        if [ ! -r "$PALACE_DEPLOY_CONF" ]; then
            printf 'rsync-palace-daemon: PALACE_DEPLOY_CONF=%s is set but not readable\n' \
                "$PALACE_DEPLOY_CONF" >&2
            exit 1
        fi
        # shellcheck disable=SC1090
        . "$PALACE_DEPLOY_CONF"
        CONF_LOADED="$PALACE_DEPLOY_CONF"
        return 0
    fi
    local candidates=(
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

# Auto-detect local dir from git toplevel if not overridden.
LOCAL_DIR="${PALACE_LOCAL_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"
REMOTE_DIR="${PALACE_REMOTE_DIR:-$LOCAL_DIR}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ -n "$HOST" ] || fail "no deploy host — set PALACE_HOST (or a deploy.conf)"
[ -n "$URL" ] || fail "no daemon URL — set PALACE_DAEMON_URL or PALACE_HOST"
[ -d "$LOCAL_DIR" ] || fail "PALACE_LOCAL_DIR not a directory: $LOCAL_DIR"
[ -n "$CONF_LOADED" ] && warn "loaded config: $CONF_LOADED"

TOTAL=3
[ "$RUN_VERIFY" = "1" ] && TOTAL=$((TOTAL + 1))
n=0
nstep() { n=$((n + 1)); step "$n/$TOTAL  $1"; }

# ---------------------------------------------------------------- 1: rsync
nstep "rsync $LOCAL_DIR/ → $SSH_TARGET:$REMOTE_DIR/"
# Exclude list matches rsync-mempalace.sh + adds palace-daemon-specific paths
# (the tests are in tests/ which we DO want to sync; venv/ and .claude/ we don't).
if ! rsync -az --delete \
    --exclude='.git/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.venv/' \
    --exclude='venv/' \
    --exclude='.pytest_cache/' \
    --exclude='*.egg-info/' \
    --exclude='.coverage' \
    --exclude='.claude/' \
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
    # Single curl — same pattern as rsync-mempalace.sh after the #95 Gemini fix.
    health=$(curl -fs --max-time 3 "$URL/health" 2>/dev/null) || true
    if [ -n "$health" ]; then
        version=$(printf '%s' "$health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version","?"))' 2>/dev/null || echo "?")
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

printf '\n\033[1;32m✦ palace-daemon rsync deploy complete: %s on %s\033[0m\n' "$LOCAL_DIR" "$URL"
