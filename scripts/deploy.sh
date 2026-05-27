#!/usr/bin/env bash
# deploy.sh — push palace-daemon, restart on the daemon host, smoke-test.
#
# A parameterised three-step deploy: git push → restart the service on the
# deploy host → poll /health → run verify-routes. Every host/path/auth value
# is configurable via env vars (or an optional config file) so the script is
# portable; the defaults below assume a simple "push to origin, ssh in,
# systemctl restart" setup.
#
# Configuration (env var → default):
#   PALACE_HOST              ssh target running the daemon          (required unless PALACE_DAEMON_URL is set)
#   PALACE_DAEMON_URL        base URL to poll for /health           (http://${PALACE_HOST}:8085)
#   PALACE_API_KEY           x-api-key for verify-routes            (empty → no auth header)
#   PALACE_SSH_USER          ssh user (prefixed to PALACE_HOST)     (none — uses your ssh config)
#   PALACE_REMOTE_DIR        repo path on the deploy host           (mirrors $PWD git toplevel)
#   PALACE_RESTART_CMD       command to restart the service         (sudo systemctl restart palace-daemon)
#   PALACE_GIT_REMOTE        git remote to push to                  (origin)
#   PALACE_GIT_BRANCH        branch to push                         (current branch)
#   PALACE_SYNC_GRACE        seconds to wait for the host to see    (0)
#                            the push (set >0 for Syncthing/rsync
#                            mirrors where the host isn't a git remote)
#   PALACE_HEALTH_TIMEOUT    seconds to wait for /health            (30)
#   PALACE_VERIFY            run verify-routes after restart (1/0)  (1)
#   PALACE_PRE_RESTART_HOOK  optional script run on the host before (none)
#                            the restart, via ssh (e.g. to sync an
#                            out-of-tree dependency's git state).
#                            Receives no args; runs in a login shell.
#
# A site-local config file may set any of the above without exporting them
# into your shell. Searched in order, first found wins:
#   $PALACE_DEPLOY_CONF (if set), ./scripts/deploy.conf,
#   $XDG_CONFIG_HOME/palace-daemon/deploy.conf, ~/.config/palace-daemon/deploy.conf
#
# Usage:
#   scripts/deploy.sh                       # uses config / env defaults
#   PALACE_HOST=myhost scripts/deploy.sh
#   PALACE_DAEMON_URL=http://10.0.0.5:8085 PALACE_HOST=10.0.0.5 scripts/deploy.sh

# We intentionally interpolate operator-controlled config (REMOTE_DIR,
# RESTART_CMD, PRE_RESTART_HOOK) into the remote ssh command line so it
# expands on the host. These come from a trusted config file, not user input.
# shellcheck disable=SC2029
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"

# ---------------------------------------------------------------- config file
# Source the first config file found. It can set any PALACE_* var below.
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

# Default the daemon URL from the host if not given explicitly.
if [ -n "${PALACE_DAEMON_URL:-}" ]; then
    URL="$PALACE_DAEMON_URL"
elif [ -n "$HOST" ]; then
    URL="http://${HOST}:8085"
else
    URL=""
fi

KEY="${PALACE_API_KEY:-}"
GIT_REMOTE="${PALACE_GIT_REMOTE:-origin}"
GIT_BRANCH="${PALACE_GIT_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
SYNC_GRACE="${PALACE_SYNC_GRACE:-0}"
HEALTH_TIMEOUT="${PALACE_HEALTH_TIMEOUT:-30}"
RESTART_CMD="${PALACE_RESTART_CMD:-sudo systemctl restart palace-daemon}"
RUN_VERIFY="${PALACE_VERIFY:-1}"
PRE_RESTART_HOOK="${PALACE_PRE_RESTART_HOOK:-}"

# Remote repo dir: default to the same path as the local git toplevel, which
# is correct for Syncthing/rsync mirrors that preserve the path. Override with
# PALACE_REMOTE_DIR for differing layouts.
REMOTE_DIR="${PALACE_REMOTE_DIR:-$(git rev-parse --show-toplevel 2>/dev/null || echo "$PWD")}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

[ -n "$SSH_TARGET" ] || fail "no deploy host — set PALACE_HOST (or a deploy.conf)"
[ -n "$URL" ] || fail "no daemon URL — set PALACE_DAEMON_URL or PALACE_HOST"
[ -n "$CONF_LOADED" ] && warn "loaded config: $CONF_LOADED"

TOTAL=5
[ "$RUN_VERIFY" = "1" ] && TOTAL=6
[ -n "$PRE_RESTART_HOOK" ] && TOTAL=$((TOTAL + 1))
n=0
nstep() { n=$((n + 1)); step "$n/$TOTAL  $1"; }

nstep "push to $GIT_REMOTE/$GIT_BRANCH"
local_sha=$(git rev-parse HEAD)
git push "$GIT_REMOTE" "$GIT_BRANCH" >/dev/null 2>&1 || fail "git push failed"
ok "pushed $local_sha → $GIT_REMOTE/$GIT_BRANCH"

nstep "confirm $HOST has the push"
# If the host is a git checkout we can compare SHAs; otherwise (Syncthing /
# rsync mirror) we just give it SYNC_GRACE seconds and trust the mirror.
[ "$SYNC_GRACE" -gt 0 ] && sleep "$SYNC_GRACE"
remote_sha=$(ssh "$SSH_TARGET" "cd '$REMOTE_DIR' && git rev-parse HEAD" 2>/dev/null || echo "")
if [ "$remote_sha" = "$local_sha" ]; then
    ok "remote at $local_sha"
elif [ -z "$remote_sha" ]; then
    ok "remote is not a git checkout — assuming mirrored deploy"
else
    warn "remote at $remote_sha (expected $local_sha)"
    if [ "$SYNC_GRACE" -gt 0 ]; then
        warn "mirror may need more time; sleeping ${SYNC_GRACE}s and retrying"
        sleep "$SYNC_GRACE"
        remote_sha=$(ssh "$SSH_TARGET" "cd '$REMOTE_DIR' && git rev-parse HEAD" 2>/dev/null || echo "")
        if [ "$remote_sha" = "$local_sha" ]; then ok "remote caught up to $local_sha"; else fail "sync lag persists; aborting"; fi
    else
        fail "remote SHA mismatch and PALACE_SYNC_GRACE=0; aborting"
    fi
fi

if [ -n "$PRE_RESTART_HOOK" ]; then
    nstep "pre-restart hook on $HOST"
    # The hook path is interpreted on the remote host. Single-quote so it
    # expands there, not locally.
    if ssh "$SSH_TARGET" "bash -lc '$PRE_RESTART_HOOK'"; then
        ok "hook ran"
    else
        warn "pre-restart hook reported non-zero (non-fatal)"
    fi
fi

nstep "restart daemon on $HOST"
ssh "$SSH_TARGET" "$RESTART_CMD" || fail "restart failed"
ok "restart issued"

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

if [ "$RUN_VERIFY" = "1" ]; then
    nstep "smoke-test routes"
    PALACE_DAEMON_URL="$URL" PALACE_API_KEY="$KEY" \
        bash "$SCRIPT_DIR/verify-routes.sh" \
        || fail "verify-routes reported failures (see output above)"
fi

printf '\n\033[1;32m✦ deploy complete: %s on %s\033[0m\n' "$local_sha" "$URL"
