#!/usr/bin/env bash
# bench-lock.sh — toggle the daemon's "bench mode": pause auto-mine AND free
# the mining model's RAM, in one operation.
#
# Bench runners (SME LongMemEval, candidate-strategy ablation, etc.) call
# this around their runs. While the lock is present:
#   - palace-daemon's WatcherService skips spawning `mempalace mine`
#     (log entry `auto_mine_paused`), and
#   - POST /mine returns a 200 skipped-response instead of spawning a
#     subprocess (palace-daemon#190 — hook-driven mines were the path that
#     actually disrupted benches; the lock now gates them too).
# See #104 for the original lock contract, #190 for the /mine gate.
#
# palace-daemon#190 extension: `acquire` ALSO stops the mining model
# (llama-server-extractor) and `release` restarts it. On a 15 GiB host the
# extractor (~3.3 GiB Phi-4-mini) + daemon + postgres + bench load tips
# earlyoom into a postgres kill-loop that silently zeroes the bench. Since
# mining is paused anyway while the lock is held, the extractor is dead
# weight during a bench — freeing its RAM gives the bench real headroom.
# Stopping the model is BEST-EFFORT: the lock is the primary mining-gate
# contract, so a systemctl failure warns but never fails the lock op.
#
# Default lock path: $PALACE_BENCH_LOCK_PATH if set, else
# $PALACE_DATA/.bench-active.lock if PALACE_DATA is set, else
# /srv/mempalace-data/palace/.bench-active.lock (the deployed default).
#
# Usage (run on the daemon host — the lock path + extractor service are local):
#   scripts/bench-lock.sh acquire           # lock + stop mining model
#   scripts/bench-lock.sh release           # unlock + restart mining model
#   scripts/bench-lock.sh status            # report lock + model state
#
# Tunables:
#   PALACE_EXTRACTOR_SERVICE       systemd unit for the mining model
#                                  (default: llama-server-extractor)
#   PALACE_BENCH_MANAGE_EXTRACTOR  set 0 to leave the model alone and only
#                                  toggle the lock (default: 1)
#
# The lock has a max-age of 6h (PALACE_BENCH_LOCK_MAX_AGE_SECONDS); the
# daemon auto-treats older locks as stale. `release` doesn't check age —
# it removes the file regardless.

set -euo pipefail

DEFAULT_LOCK="/srv/mempalace-data/palace/.bench-active.lock"
LOCK_PATH="${PALACE_BENCH_LOCK_PATH:-${PALACE_DATA:+$PALACE_DATA/.bench-active.lock}}"
LOCK_PATH="${LOCK_PATH:-$DEFAULT_LOCK}"

EXTRACTOR_SERVICE="${PALACE_EXTRACTOR_SERVICE:-llama-server-extractor}"
MANAGE_EXTRACTOR="${PALACE_BENCH_MANAGE_EXTRACTOR:-1}"

# Is the extractor unit known to systemd on this host? Guards the model
# management so the script is a clean no-op on hosts without it (or when
# run somewhere systemctl isn't available).
_extractor_known() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl cat "${EXTRACTOR_SERVICE}.service" >/dev/null 2>&1
}

_extractor_state() {
    systemctl is-active "${EXTRACTOR_SERVICE}.service" 2>/dev/null || echo "unknown"
}

# Best-effort stop/start. Never `exit` on failure — the lock is the primary
# mining-gate contract; freeing the model is an optimization layered on top.
_stop_extractor() {
    [ "$MANAGE_EXTRACTOR" = "1" ] || return 0
    if ! _extractor_known; then
        printf 'bench-lock: extractor %s not present here — skipping model stop\n' "$EXTRACTOR_SERVICE"
        return 0
    fi
    if sudo systemctl stop "${EXTRACTOR_SERVICE}.service" 2>/dev/null; then
        printf 'bench-lock: stopped %s (freed mining-model RAM)\n' "$EXTRACTOR_SERVICE"
    else
        printf 'bench-lock: WARN could not stop %s (continuing — lock still gates mining)\n' \
            "$EXTRACTOR_SERVICE" >&2
    fi
}

_start_extractor() {
    [ "$MANAGE_EXTRACTOR" = "1" ] || return 0
    if ! _extractor_known; then
        return 0
    fi
    if sudo systemctl start "${EXTRACTOR_SERVICE}.service" 2>/dev/null; then
        printf 'bench-lock: started %s (mining model restored)\n' "$EXTRACTOR_SERVICE"
    else
        printf 'bench-lock: WARN could not start %s (start it manually: sudo systemctl start %s)\n' \
            "$EXTRACTOR_SERVICE" "$EXTRACTOR_SERVICE" >&2
    fi
}

acquire() {
    mkdir -p "$(dirname "$LOCK_PATH")" 2>/dev/null || true
    if ! touch "$LOCK_PATH"; then
        printf 'bench-lock: cannot touch %s (check permissions)\n' "$LOCK_PATH" >&2
        exit 1
    fi
    printf 'bench-lock: acquired %s\n' "$LOCK_PATH"
    _stop_extractor
}

release() {
    if [ ! -e "$LOCK_PATH" ]; then
        printf 'bench-lock: no lock to release (%s does not exist)\n' "$LOCK_PATH"
    elif ! rm -f "$LOCK_PATH"; then
        printf 'bench-lock: cannot remove %s\n' "$LOCK_PATH" >&2
        exit 1
    else
        printf 'bench-lock: released %s\n' "$LOCK_PATH"
    fi
    # Restart the model even if the lock was already gone — `release` means
    # "exit bench mode", and leaving the mining model down would be a silent
    # surprise (no auto-mine capability until someone notices).
    _start_extractor
}

status() {
    if [ ! -e "$LOCK_PATH" ]; then
        printf 'bench-lock: absent at %s\n' "$LOCK_PATH"
    else
        age=$(($(date +%s) - $(stat -c %Y "$LOCK_PATH" 2>/dev/null || stat -f %m "$LOCK_PATH")))
        printf 'bench-lock: present at %s (age %ds)\n' "$LOCK_PATH" "$age"
    fi
    if [ "$MANAGE_EXTRACTOR" = "1" ] && _extractor_known; then
        printf 'bench-lock: mining model %s is %s\n' "$EXTRACTOR_SERVICE" "$(_extractor_state)"
    fi
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
