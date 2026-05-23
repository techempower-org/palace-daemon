#!/usr/bin/env bash
# deploy.sh — push palace-daemon main, restart on the daemon host, smoke-test.
#
# Assumes:
#   - You're committed and want to push HEAD to origin/main.
#   - The deploy host has the repo synced (e.g., via Syncthing).
#   - palace-daemon is a **systemd system** service named "palace-daemon"
#     (unit at /etc/systemd/system/palace-daemon.service, restarted via
#     `sudo systemctl restart palace-daemon`). User-level units are NOT
#     used and must not be created. A user unit alongside the system
#     unit will cause both to `ExecStartPre=/usr/bin/fuser -k 8085/tcp`
#     each other in a kill cascade (restart counter ran to 97 before
#     the duplicate user unit was deleted, 2026-05-16). See
#     palace-daemon/CLAUDE.md "Service unit" section.
#   - The daemon venv lives at ~/.local/share/palace-daemon/venv/
#     and is preferentially managed with uv (`uv pip install --python
#     <venv-python> ...`). Stdlib venv usage is legacy. Either path
#     works for installs since the venv's pip works.
#
# Usage:
#   scripts/deploy.sh                       # default host: disks
#   PALACE_HOST=otherhost scripts/deploy.sh

set -euo pipefail

HOST="${PALACE_HOST:-disks}"
URL="${PALACE_DAEMON_URL:-http://${HOST}.jphe.in:8085}"
KEY="${PALACE_API_KEY:-}"

# Fallback: read PALACE_API_KEY from ~/.claude/settings.local.json env block
# (the source of truth managed by palace-mode).
if [ -z "$KEY" ] && [ -f "$HOME/.claude/settings.local.json" ]; then
    KEY=$(python3 -c "
import json
d = json.load(open('$HOME/.claude/settings.local.json'))
print(d.get('env', {}).get('PALACE_API_KEY', ''))
" 2>/dev/null || echo "")
fi
SYNC_GRACE="${PALACE_SYNC_GRACE:-3}"   # seconds to let Syncthing catch up
HEALTH_TIMEOUT="${PALACE_HEALTH_TIMEOUT:-30}"

step() { printf '\n\033[1m▸ %s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

step "1/6  push to origin"
local_sha=$(git rev-parse HEAD)
git push origin main >/dev/null 2>&1 || fail "git push failed"
ok "pushed $local_sha → origin/main"

step "2/6  wait for sync to $HOST"
sleep "$SYNC_GRACE"
remote_sha=$(ssh "$HOST" "cd /mnt/raid/projects/palace-daemon && git rev-parse HEAD 2>/dev/null || git log -1 --format=%H 2>/dev/null" 2>/dev/null || echo "")
if [ "$remote_sha" = "$local_sha" ]; then
    ok "remote at $local_sha"
elif [ -z "$remote_sha" ]; then
    ok "remote is not a git checkout — assuming Syncthing-only mirror"
else
    echo "  ! remote at $remote_sha (expected $local_sha)"
    echo "  ! Syncthing may need more time; sleeping ${SYNC_GRACE}s and retrying"
    sleep "$SYNC_GRACE"
    remote_sha=$(ssh "$HOST" "cd /mnt/raid/projects/palace-daemon && git rev-parse HEAD" 2>/dev/null || echo "")
    [ "$remote_sha" = "$local_sha" ] && ok "remote caught up to $local_sha" || fail "sync lag persists; aborting"
fi

step "3/6  sync memorypalace git state on $HOST"
# Syncthing keeps the working tree in sync, but .git is excluded from sync
# (.stignore). The editable install reads the working tree so Python sees the
# right code, but git HEAD drifts behind. Fix that here so `git log` on disks
# is consistent and `git pull` doesn't choke on "local changes" next time.
MEMPALACE_DIR="/mnt/raid/projects/memorypalace"
ssh "$HOST" "cd $MEMPALACE_DIR && git fetch origin --quiet 2>/dev/null && git reset --hard origin/main --quiet 2>/dev/null" \
    && ok "memorypalace git synced to origin/main" \
    || echo "  ! memorypalace git sync skipped (non-fatal)"

step "4/6  restart palace-daemon on $HOST"
# System service, not user service. sudo without password requires passwordless
# sudo on the target (jp has this on the homelab hosts per CLAUDE.md).
ssh "$HOST" "sudo systemctl restart palace-daemon" || fail "restart failed"
ok "restart issued"

step "5/6  wait for daemon health"
deadline=$((SECONDS + HEALTH_TIMEOUT))
while (( SECONDS < deadline )); do
    if curl -fs --max-time 3 "$URL/health" >/dev/null 2>&1; then
        version=$(curl -s "$URL/health" | python3 -c 'import sys,json; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo "?")
        ok "healthy on v$version (after $((SECONDS - (deadline - HEALTH_TIMEOUT)))s)"
        break
    fi
    sleep 1
done
(( SECONDS >= deadline )) && fail "daemon did not respond on $URL within ${HEALTH_TIMEOUT}s"

step "6/6  smoke-test routes"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
PALACE_DAEMON_URL="$URL" PALACE_API_KEY="$KEY" \
    bash "$SCRIPT_DIR/verify-routes.sh" \
    || fail "verify-routes reported failures (see output above)"

printf '\n\033[1;32m✦ deploy complete: %s on %s\033[0m\n' "$local_sha" "$URL"
