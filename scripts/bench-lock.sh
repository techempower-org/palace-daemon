#!/usr/bin/env bash
# bench-lock.sh — touch / remove the daemon's bench-active lock file.
#
# Bench runners (SME LongMemEval, candidate-strategy ablation, etc.) call
# this around their runs to pause palace-daemon's auto-mine. While the lock
# is present the daemon's WatcherService skips spawning `mempalace mine`
# subprocesses, log entry `auto_mine_paused`. See #104 for the contract.
#
# Default lock path: $PALACE_BENCH_LOCK_PATH if set, else
# $PALACE_DATA/.bench-active.lock if PALACE_DATA is set, else
# /srv/mempalace-data/palace/.bench-active.lock (the deployed default).
#
# Usage:
#   scripts/bench-lock.sh acquire           # touch the lock
#   scripts/bench-lock.sh release           # remove the lock
#   scripts/bench-lock.sh status            # report present/absent + age
#
# The lock has a max-age of 6h (PALACE_BENCH_LOCK_MAX_AGE_SECONDS); the
# daemon auto-treats older locks as stale. `release` doesn't check age —
# it removes the file regardless.

set -euo pipefail

DEFAULT_LOCK="/srv/mempalace-data/palace/.bench-active.lock"
LOCK_PATH="${PALACE_BENCH_LOCK_PATH:-${PALACE_DATA:+$PALACE_DATA/.bench-active.lock}}"
LOCK_PATH="${LOCK_PATH:-$DEFAULT_LOCK}"

acquire() {
    mkdir -p "$(dirname "$LOCK_PATH")" 2>/dev/null || true
    if ! touch "$LOCK_PATH"; then
        printf 'bench-lock: cannot touch %s (check permissions)\n' "$LOCK_PATH" >&2
        exit 1
    fi
    printf 'bench-lock: acquired %s\n' "$LOCK_PATH"
}

release() {
    if [ ! -e "$LOCK_PATH" ]; then
        printf 'bench-lock: no lock to release (%s does not exist)\n' "$LOCK_PATH"
        return 0
    fi
    if ! rm -f "$LOCK_PATH"; then
        printf 'bench-lock: cannot remove %s\n' "$LOCK_PATH" >&2
        exit 1
    fi
    printf 'bench-lock: released %s\n' "$LOCK_PATH"
}

status() {
    if [ ! -e "$LOCK_PATH" ]; then
        printf 'bench-lock: absent at %s\n' "$LOCK_PATH"
        return 0
    fi
    age=$(($(date +%s) - $(stat -c %Y "$LOCK_PATH" 2>/dev/null || stat -f %m "$LOCK_PATH")))
    printf 'bench-lock: present at %s (age %ds)\n' "$LOCK_PATH" "$age"
}

case "${1:-status}" in
    acquire|lock|touch) acquire ;;
    release|unlock|rm)  release ;;
    status|info)        status ;;
    *)
        printf 'usage: %s {acquire|release|status}\n' "$0" >&2
        exit 2
        ;;
esac
