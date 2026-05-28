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
# Special case: if PALACE_DEPLOY_CONF is set explicitly and the file is
# missing/unreadable, fail loudly rather than silently falling back to the
# default search path — the user expressed an intent that we shouldn't
# discard. The implicit candidates fall through silently as before.
# Matches the pattern in scripts/rsync-mempalace.sh (issue #100).
_load_conf() {
    if [ -n "${PALACE_DEPLOY_CONF:-}" ]; then
        if [ ! -r "$PALACE_DEPLOY_CONF" ]; then
            printf 'deploy.sh: PALACE_DEPLOY_CONF=%s is set but not readable\n' \
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

[ -n "$HOST" ] || fail "no deploy host — set PALACE_HOST (or a deploy.conf)"
[ -n "$URL" ] || fail "no daemon URL — set PALACE_DAEMON_URL or PALACE_HOST"
[ -n "$CONF_LOADED" ] && warn "loaded config: $CONF_LOADED"

TOTAL=4
[ "$RUN_VERIFY" = "1" ] && TOTAL=$((TOTAL + 1))
[ -n "$PRE_RESTART_HOOK" ] && TOTAL=$((TOTAL + 1))
# #122: optional mempalace-db config-drift check (fires when docker-compose.yml exists)
[ -f "$(git rev-parse --show-toplevel 2>/dev/null)/mempalace-db/docker-compose.yml" ] && TOTAL=$((TOTAL + 1))
n=0
nstep() { n=$((n + 1)); step "$n/$TOTAL  $1"; }

nstep "push to $GIT_REMOTE/$GIT_BRANCH"
local_sha=$(git rev-parse HEAD)
git push "$GIT_REMOTE" "$GIT_BRANCH" >/dev/null 2>&1 || fail "git push failed"
ok "pushed $local_sha → $GIT_REMOTE/$GIT_BRANCH"

# Read the remote HEAD over ssh, distinguishing three outcomes:
#   - ssh connection failure (host down / auth)  → return 2, no output
#   - connected, but $REMOTE_DIR is not a git checkout → return 0, empty SHA
#   - connected git checkout → return 0, the SHA
# We append a sentinel line that only prints if ssh actually connected, so an
# empty SHA from a dead connection can't be mistaken for a mirror-only host.
remote_head() {
    local out sha
    out=$(ssh "$SSH_TARGET" "cd '$REMOTE_DIR' 2>/dev/null && git rev-parse HEAD 2>/dev/null; echo __SSH_OK__") || return 2
    case "$out" in
        *__SSH_OK__*) : ;;          # connected
        *)            return 2 ;;   # no sentinel → connection failed
    esac
    # The SHA (if any) is the first line; the sentinel is the rest. With no
    # SHA the output is just the sentinel, so sha resolves to empty.
    sha=$(printf '%s\n' "$out" | head -1)
    [ "$sha" = "__SSH_OK__" ] && sha=""
    printf '%s' "$sha"
}

nstep "confirm $HOST has the push"
# If the host is a git checkout we can compare SHAs; otherwise (Syncthing /
# rsync mirror) we give it SYNC_GRACE seconds and trust the mirror.
[ "$SYNC_GRACE" -gt 0 ] && sleep "$SYNC_GRACE"
if ! remote_sha=$(remote_head); then
    fail "cannot reach $SSH_TARGET over ssh — host down, auth failed, or wrong target; aborting (deploy NOT applied)"
fi
if [ "$remote_sha" = "$local_sha" ]; then
    ok "remote at $local_sha"
elif [ -z "$remote_sha" ]; then
    ok "remote is not a git checkout — assuming mirrored deploy"
else
    warn "remote at $remote_sha (expected $local_sha)"
    if [ "$SYNC_GRACE" -gt 0 ]; then
        warn "mirror may need more time; sleeping ${SYNC_GRACE}s and retrying"
        sleep "$SYNC_GRACE"
        if ! remote_sha=$(remote_head); then
            fail "cannot reach $SSH_TARGET over ssh on retry; aborting"
        fi
        if [ "$remote_sha" = "$local_sha" ]; then ok "remote caught up to $local_sha"; else fail "sync lag persists; aborting"; fi
    else
        fail "remote SHA mismatch and PALACE_SYNC_GRACE=0; aborting"
    fi
fi

if [ -n "$PRE_RESTART_HOOK" ]; then
    nstep "pre-restart hook on $HOST"
    # Feed the hook to a remote login shell on stdin rather than embedding it
    # in the ssh command string, so hooks containing single quotes don't break
    # the remote quoting.
    if ssh "$SSH_TARGET" "bash -l" <<< "$PRE_RESTART_HOOK"; then
        ok "hook ran"
    else
        warn "pre-restart hook reported non-zero (non-fatal)"
    fi
fi

# #122: Check whether mempalace-db's running container reflects the
# committed config. Today's 1.9.0 + #117 deploy showed the gap — postgres
# config (shared_buffers, mem_limit) sat in mempalace-db/ files for hours
# while the running container kept the old limits. Cgroup is hot-fixable
# via `docker update`; postgresql.conf needs a container recreate
# (maintenance window — see mempalace-db/README.md).
if [ -f "$(git rev-parse --show-toplevel 2>/dev/null)/mempalace-db/docker-compose.yml" ]; then
    nstep "check mempalace-db config drift"
    expected_mem=$(grep -oE '^\s*mem_limit:\s*[0-9]+[gG]' \
        "$(git rev-parse --show-toplevel)/mempalace-db/docker-compose.yml" 2>/dev/null \
        | head -1 | grep -oE '[0-9]+[gG]' || echo "")
    if [ -n "$expected_mem" ]; then
        # Read the running container's cgroup limit (bytes) and compare.
        actual_bytes=$(ssh "$SSH_TARGET" "docker inspect mempalace-db --format '{{.HostConfig.Memory}}' 2>/dev/null" || echo "0")
        # Compute expected bytes from the "Ng" suffix.
        expected_gb=$(printf '%s' "$expected_mem" | tr -dc '0-9')
        expected_bytes=$((expected_gb * 1024 * 1024 * 1024))
        if [ "$actual_bytes" != "$expected_bytes" ]; then
            warn "mempalace-db cgroup drift: container has $actual_bytes bytes, docker-compose.yml says $expected_mem"
            warn "Hot-fix:    ssh $HOST 'docker update --memory=$expected_mem --memory-swap=$expected_mem mempalace-db'"
            warn "Full apply: see mempalace-db/README.md (recreate during maintenance window)"
        else
            ok "cgroup matches docker-compose.yml ($expected_mem)"
        fi
    fi
    # Check whether the live postgresql.conf settings match the committed file
    # for the canary fields (shared_buffers + effective_cache_size — the ones
    # that motivated #117). A precise check would diff the whole file; this
    # catches the high-impact drifts without full-file comparison overhead.
    for setting in shared_buffers effective_cache_size; do
        expected=$(grep -E "^${setting}\s*=" \
            "$(git rev-parse --show-toplevel)/mempalace-db/postgresql.conf" 2>/dev/null \
            | head -1 | sed -E "s/^${setting}\s*=\s*([^ #]+).*/\1/")
        [ -z "$expected" ] && continue
        actual=$(ssh "$SSH_TARGET" "docker exec mempalace-db psql -U palace mempalace_2026_05_13 -tA -c \"SHOW $setting\"" 2>/dev/null | tr -d ' ')
        if [ -n "$actual" ] && [ "$actual" != "$expected" ]; then
            warn "postgresql.conf $setting drift: container=$actual, committed=$expected"
            warn "Apply: ssh $HOST 'cd ~/Projects/palace-daemon/mempalace-db && docker compose down && docker compose up -d' (maintenance window)"
        fi
    done
fi

nstep "restart daemon on $HOST"
ssh "$SSH_TARGET" "$RESTART_CMD" || fail "restart failed"
ok "restart issued"

nstep "wait for daemon health"
deadline=$((SECONDS + HEALTH_TIMEOUT))
deployed_version="?"
while (( SECONDS < deadline )); do
    # Try a 2xx fetch first. /health may return 503 during crash_loop windows
    # but still has a populated body — re-fetch without -f so we still parse
    # the version field for the verification step below.
    health=$(curl -fs --max-time 3 "$URL/health" 2>/dev/null) || true
    [ -z "$health" ] && health=$(curl -s --max-time 3 "$URL/health" 2>/dev/null) || true
    if [ -n "$health" ]; then
        deployed_version=$(printf '%s' "$health" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("version","?"))' 2>/dev/null || echo "?")
        ok "healthy on v$deployed_version (after $((SECONDS - (deadline - HEALTH_TIMEOUT)))s)"
        break
    fi
    sleep 1
done
(( SECONDS >= deadline )) && fail "daemon did not respond on $URL within ${HEALTH_TIMEOUT}s"

# #119: verify the deployed VERSION matches the local code constant.
# Today's 1.9.0 deploy (commit 3dc4d39) returned green here even though
# Syncthing hadn't delivered the new code — the daemon restarted on the
# old VERSION=1.8.4 main.py. This check turns silent failure loud.
expected_version=$(grep -E '^VERSION\s*=' "$(git rev-parse --show-toplevel)/main.py" 2>/dev/null \
    | head -1 | sed -E 's/^VERSION\s*=\s*"([^"]+)".*/\1/')
if [ -n "$expected_version" ] && [ "$deployed_version" != "?" ] && [ "$expected_version" != "$deployed_version" ]; then
    warn "VERSION mismatch: expected v$expected_version (local) but daemon reports v$deployed_version"
    warn "Code may not have synced to $HOST. Try: scripts/rsync-palace-daemon.sh"
fi

if [ "$RUN_VERIFY" = "1" ]; then
    nstep "smoke-test routes"
    PALACE_DAEMON_URL="$URL" PALACE_API_KEY="$KEY" \
        bash "$SCRIPT_DIR/verify-routes.sh" \
        || fail "verify-routes reported failures (see output above)"
fi

printf '\n\033[1;32m✦ deploy complete: %s on %s\033[0m\n' "$local_sha" "$URL"
