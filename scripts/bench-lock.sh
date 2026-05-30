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
#   scripts/bench-lock.sh acquire           # register this bench + stop model
#   scripts/bench-lock.sh release           # deregister this bench (+ restart
#                                           #   model when the LAST bench exits)
#   scripts/bench-lock.sh status            # report refcount + model state
#
# Refcount (palace-daemon#196): the lock path is now a DIRECTORY of
# per-bench PID markers (`<pid>.marker`) instead of a single boolean file.
# `acquire` mkdir's the directory and drops this process's marker; `release`
# removes ONLY this process's marker. Auto-mine stays paused while ≥1 marker
# is present, and the extractor is stopped on the 0→1 transition and started
# on the 1→0 transition — so N concurrent benches share the lock correctly
# and a finishing bench no longer un-pauses mining under a still-running one.
# Marker PID defaults to $$ (this shell); override with PALACE_BENCH_PID — an
# SSH'd-in bench should pass the PID it wants reaped (its own remote PID).
#
# Stale markers (older than PALACE_BENCH_LOCK_MAX_AGE_SECONDS, default 6h, or
# whose host-local PID is dead) are reaped on every acquire/release/status so
# a crashed bench can't wedge auto-mine. A pre-existing plain-FILE lock (the
# legacy #104 shape) is migrated to the directory form on first acquire.
#
# Tunables:
#   PALACE_BENCH_PID               PID to record in this marker (default: $$)
#   PALACE_EXTRACTOR_SERVICE       systemd unit for the mining model
#                                  (default: llama-server-extractor)
#   PALACE_BENCH_MANAGE_EXTRACTOR  set 0 to leave the model alone and only
#                                  toggle the lock (default: 1)
#   PALACE_BENCH_LOCK_MAX_AGE_SECONDS  stale-marker threshold (default: 21600)

set -euo pipefail

DEFAULT_LOCK="/srv/mempalace-data/palace/.bench-active.lock"
LOCK_PATH="${PALACE_BENCH_LOCK_PATH:-${PALACE_DATA:+$PALACE_DATA/.bench-active.lock}}"
LOCK_PATH="${LOCK_PATH:-$DEFAULT_LOCK}"

# Refcount marker for THIS bench. Default to the shell PID; an SSH'd-in bench
# can override so the marker carries a PID meaningful for reaping.
MARKER_PID="${PALACE_BENCH_PID:-$$}"
MARKER_FILE="${LOCK_PATH}/${MARKER_PID}.marker"
MAX_AGE="${PALACE_BENCH_LOCK_MAX_AGE_SECONDS:-21600}"

EXTRACTOR_SERVICE="${PALACE_EXTRACTOR_SERVICE:-llama-server-extractor}"
MANAGE_EXTRACTOR="${PALACE_BENCH_MANAGE_EXTRACTOR:-1}"

# --- refcount directory helpers (palace-daemon#196) -------------------------

# Migrate a pre-existing legacy plain-FILE lock into the directory form. A
# bench that touched the old single-file lock keeps blocking auto-mine: we
# preserve that by converting it to one anonymous "legacy" marker so the
# directory's refcount starts at ≥1 until that bench releases.
_migrate_legacy_file() {
    if [ -e "$LOCK_PATH" ] && [ ! -d "$LOCK_PATH" ]; then
        local mtime
        mtime=$(stat -c %Y "$LOCK_PATH" 2>/dev/null || stat -f %m "$LOCK_PATH" 2>/dev/null || echo 0)
        rm -f "$LOCK_PATH" 2>/dev/null || true
        mkdir -p "$LOCK_PATH" 2>/dev/null || return 1
        # Preserve the legacy lock's age so its staleness still expires.
        : > "${LOCK_PATH}/legacy.marker" 2>/dev/null || true
        if [ "$mtime" -gt 0 ] 2>/dev/null; then
            touch -d "@${mtime}" "${LOCK_PATH}/legacy.marker" 2>/dev/null || true
        fi
        printf 'bench-lock: migrated legacy file lock → refcount dir %s\n' "$LOCK_PATH"
    fi
}

# Reap markers stale-by-AGE only, and echo the count of LIVE markers.
# We do NOT reap on PID-liveness: SME benches SSH in from katana and record
# their katana PID, which isn't in this host's process table — a `kill -0`
# here would wrongly reap a live remote bench. The age guard (default 6 h,
# matching the daemon's PALACE_BENCH_LOCK_MAX_AGE_SECONDS) is the backstop; a
# long bench refreshes its marker mtime via a heartbeat `touch`.
_reap_and_count() {
    [ -d "$LOCK_PATH" ] || { echo 0; return 0; }
    local now live=0 m age mtime
    now=$(date +%s)
    for m in "$LOCK_PATH"/*.marker; do
        [ -e "$m" ] || continue
        mtime=$(stat -c %Y "$m" 2>/dev/null || stat -f %m "$m" 2>/dev/null || echo "$now")
        age=$(( now - mtime ))
        if [ "$age" -gt "$MAX_AGE" ]; then
            rm -f "$m" 2>/dev/null || true
            continue
        fi
        live=$(( live + 1 ))
    done
    echo "$live"
}

# Is the extractor unit known to systemd on this host? Guards the model
# management so the script is a clean no-op on hosts without it (or when
# run somewhere systemctl isn't available).
_extractor_known() {
    command -v systemctl >/dev/null 2>&1 || return 1
    systemctl cat "${EXTRACTOR_SERVICE}.service" >/dev/null 2>&1
}

_extractor_state() {
    # `systemctl is-active` prints the accurate state (active/inactive/
    # failed/unknown) but exits non-zero for anything but "active" — so we
    # swallow the exit code with `|| true` rather than appending our own
    # word (which previously leaked a spurious second "unknown" line).
    systemctl is-active "${EXTRACTOR_SERVICE}.service" 2>/dev/null || true
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
    _migrate_legacy_file
    if ! mkdir -p "$LOCK_PATH" 2>/dev/null; then
        printf 'bench-lock: cannot create lock dir %s (check permissions)\n' "$LOCK_PATH" >&2
        exit 1
    fi
    # Count live markers BEFORE registering, to detect the 0→1 transition
    # (only the first bench stops the extractor).
    local before
    before=$(_reap_and_count)
    if ! touch "$MARKER_FILE"; then
        printf 'bench-lock: cannot write marker %s (check permissions)\n' "$MARKER_FILE" >&2
        exit 1
    fi
    local after
    after=$(_reap_and_count)
    printf 'bench-lock: acquired marker %s (refcount %d → %d)\n' "$MARKER_FILE" "$before" "$after"
    if [ "$before" -eq 0 ]; then
        _stop_extractor
    else
        printf 'bench-lock: %d other bench(es) already registered — model left as-is\n' "$before"
    fi
}

release() {
    if [ ! -d "$LOCK_PATH" ]; then
        # Legacy plain-file lock left by an old caller — remove it and treat
        # as a full exit (refcount 0).
        if [ -e "$LOCK_PATH" ]; then
            rm -f "$LOCK_PATH" 2>/dev/null \
                && printf 'bench-lock: removed legacy file lock %s\n' "$LOCK_PATH"
        else
            printf 'bench-lock: no lock to release (%s absent)\n' "$LOCK_PATH"
        fi
        _start_extractor
        return 0
    fi
    if [ -e "$MARKER_FILE" ]; then
        rm -f "$MARKER_FILE" 2>/dev/null \
            && printf 'bench-lock: released marker %s\n' "$MARKER_FILE" \
            || printf 'bench-lock: WARN could not remove %s\n' "$MARKER_FILE" >&2
    else
        printf 'bench-lock: this bench had no marker (%s absent) — deregistering anyway\n' "$MARKER_FILE"
    fi
    local remaining
    remaining=$(_reap_and_count)
    printf 'bench-lock: refcount now %d\n' "$remaining"
    if [ "$remaining" -eq 0 ]; then
        # Last bench out: drop the (now-empty) dir and restore the model.
        rmdir "$LOCK_PATH" 2>/dev/null || true
        _start_extractor
    else
        printf 'bench-lock: %d bench(es) still registered — auto-mine stays paused, model left down\n' "$remaining"
    fi
}

status() {
    if [ ! -e "$LOCK_PATH" ]; then
        printf 'bench-lock: absent at %s (refcount 0)\n' "$LOCK_PATH"
    elif [ -d "$LOCK_PATH" ]; then
        local live
        live=$(_reap_and_count)
        printf 'bench-lock: refcount dir %s — %d live bench(es)\n' "$LOCK_PATH" "$live"
        for m in "$LOCK_PATH"/*.marker; do
            [ -e "$m" ] || continue
            local base age mtime now
            base=$(basename "$m"); now=$(date +%s)
            mtime=$(stat -c %Y "$m" 2>/dev/null || stat -f %m "$m" 2>/dev/null || echo "$now")
            age=$(( now - mtime ))
            printf '  - %s (age %ds)\n' "${base%.marker}" "$age"
        done
    else
        age=$(($(date +%s) - $(stat -c %Y "$LOCK_PATH" 2>/dev/null || stat -f %m "$LOCK_PATH")))
        printf 'bench-lock: legacy file lock at %s (age %ds)\n' "$LOCK_PATH" "$age"
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
