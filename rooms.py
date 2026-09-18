"""Wing-slug + canonical-rooms validation — extracted from main.py per #101 (twelfth slice).

Owns two pre-write validation surfaces used at the /memory boundary:

1. ``normalize_wing_slug(s)`` — canonical wing-slug form per the
   2026-05-14 taxonomy spec §3.2. Idempotent. Used so writes from
   any caller (familiar, manual curl, test rigs) land with the same
   slug shape as the miner produces.

2. ``canonical_rooms()`` + ``_canonical_rooms_cache`` — the
   configurable room set, read lazily from ``mempalace_canonical_rooms``
   and cached for the daemon's lifetime. Invalidated by
   ``daemon_tools.invalidate_rooms_cache`` (called from the rooms
   CRUD handlers) and by POST /admin/refresh-rooms.

main.py re-exports under ``_``-prefixed names. Tests that mutate the
cache state directly (``main._canonical_rooms_cache = X``) have been
updated to mutate ``rooms._canonical_rooms_cache`` because module-level
attribute writes don't propagate through re-exports.
"""
from __future__ import annotations

import os
import re


def normalize_wing_slug(s: str) -> str:
    """Canonical wing-slug form per the 2026-05-14 taxonomy spec §3.2.

    Idempotent: applying twice yields the same result. Used at the
    /memory boundary so writes from any caller (familiar, manual curl,
    test rigs) land with the same slug shape as the miner produces.
    """
    if not s:
        return "unknown"
    s = s.lower()
    if s.startswith("wing_"):
        s = s[5:]
    s = re.sub(r"[^a-z0-9_]", "_", s)
    return s or "unknown"


# Cached set of canonical room names. Populated lazily on first /memory
# write; invalidate via POST /admin/refresh-rooms after registering a new
# canonical room (e.g. `mempalace rooms add`). Otherwise cached for the
# daemon's lifetime.
_canonical_rooms_cache: set[str] | None = None


def canonical_rooms() -> set[str]:
    """Read the configurable room set from mempalace_canonical_rooms.

    Falls back to the spec's default 7 when the lookup table is absent
    or the backend isn't postgres (legacy chroma path doesn't have the
    FK lookup; validate against the spec defaults).
    """
    global _canonical_rooms_cache
    if _canonical_rooms_cache is not None:
        return _canonical_rooms_cache

    DEFAULTS = {"architecture", "decisions", "problems", "planning",
                "sessions", "references", "discoveries"}

    try:
        # Lazy mempalace.config lookup — done here rather than at module
        # load so importing this module doesn't pull mempalace into the
        # import graph just for tests that don't need it.
        import mempalace.mcp_server as _mp
        if _mp._config.backend != "postgres":
            _canonical_rooms_cache = DEFAULTS
            return _canonical_rooms_cache
        import psycopg2
        dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
        if not dsn:
            _canonical_rooms_cache = DEFAULTS
            return _canonical_rooms_cache
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT name FROM mempalace_canonical_rooms")
                rows = cur.fetchall()
        if rows:
            _canonical_rooms_cache = {r[0] for r in rows}
        else:
            _canonical_rooms_cache = DEFAULTS
    except Exception as e:
        # Schema drift / postgres unavailable — fall back to spec defaults.
        # Log so the underlying issue surfaces (lesson from #157: silent
        # except: pass hides bugs for weeks).
        import logging
        logging.warning("canonical_rooms: lookup failed, falling back to spec defaults: %s", e)
        _canonical_rooms_cache = DEFAULTS
    return _canonical_rooms_cache


# Cached set of rooms that actually HOLD drawers. Unlike the canonical
# set this is not configuration — it is a fact about the data, and it
# grows whenever mempalace's miner writes a drawer straight to postgres,
# which is how `diary` (38,781) and `general` (36,838) got there without
# ever passing through the daemon's write endpoints. There is therefore
# no write hook to invalidate on, which is why the read validator
# RE-READS on a miss rather than trusting a TTL: a stale cache must never
# produce a 400 for a room that now exists — that is the defect (#285).
_present_rooms_cache: "set[str] | None" = None


def _read_present_rooms() -> "set[str] | None":
    """One query: rooms that hold drawers, or ``None`` if it cannot run.

    Split from ``present_rooms`` so the cache logic and the read can be
    exercised separately — the "a stale cache re-reads before refusing"
    test has to make the second read return something the first did not.

    Keyed on ``MEMPALACE_POSTGRES_DSN`` alone, which is how the daemon's
    other postgres readers do it (``search_routes``, ``kg_reader``), and
    deliberately NOT on ``mempalace``'s ``_config.backend`` the way
    ``canonical_rooms`` does. That check resolves the backend from a palace
    path, so it answers "chroma" in any process that lacks the daemon's own
    environment — and since this validator fails open, that would silently
    delete the typo protection instead of failing loudly. Measured while
    building this: a probe outside the daemon's EnvironmentFile reported
    `backend='chroma'` and every room, typos included, passed. Not importing
    mempalace here also keeps this path clear of the PYTHONPATH-stripping
    that ``mempalace/__init__`` performs on import.
    """
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        return None
    try:
        import psycopg2
        with psycopg2.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT DISTINCT room FROM mempalace_drawers WHERE room IS NOT NULL"
                )
                rows = cur.fetchall()
        return {r[0] for r in rows}
    except Exception as e:
        # Surface it, same rule as canonical_rooms (#157: a silent except
        # hides bugs for weeks) — but NOT the same fallback. ``None`` means
        # "cannot tell", and the caller must not refuse on that.
        import logging
        logging.warning("present_rooms: lookup failed, room filters unvalidated: %s", e)
        return None


def present_rooms(refresh: bool = False) -> "set[str] | None":
    """Rooms holding at least one drawer — or ``None`` when unknowable.

    ``None`` is deliberate and is not ``set()``: an empty set says "the
    corpus has no rooms", ``None`` says "the lookup could not run".
    Measured against production: 129 ms for 15 rows over 924,568 drawers,
    so it is cached, and consulted only on the non-canonical path.
    """
    global _present_rooms_cache
    if _present_rooms_cache is not None and not refresh:
        return _present_rooms_cache
    found = _read_present_rooms()
    if found is not None:
        _present_rooms_cache = found
        return found
    # The read failed. A cache we already populated is still the best
    # knowledge available — returning None here would throw it away and
    # answer "cannot tell" while holding a good answer, which turns every
    # transient DB hiccup into silently accepting typos.
    return _present_rooms_cache


def validate_room_filter_or_raise(room):
    """Read-side room validation: refuse only a room that exists NOWHERE.

    The read counterpart to ``validate_room_or_raise``, which stays the
    write guard. This module already draws exactly this split for wings —
    ``normalize_wing_slug`` (write) vs ``normalize_wing_filter`` (read) —
    and says why a filter and a write cannot share one rule. Rooms never
    got it, so the write rule ("only canonical rooms may be CREATED") was
    enforced on reads, where it means "data that exists may not be read":
    measured at 79,889 drawers, 8.6% of the corpus, unreachable by filter
    (#285).

    Canonical passes, unchanged. A room that exists passes — the fix. A
    room that exists nowhere still raises 400, which is what the check was
    built for per ``validate_room_or_raise``'s own docstring: "fast-feedback
    room validation (vs an empty-result silent surprise from a typo)". So
    ``room=diaryy`` still fails fast; ``room=diary`` no longer does.

    Existence is corpus-wide, not per-wing, on purpose: per-wing would 400
    ``?wing=2g&room=references`` when 2g merely holds none, turning a
    legitimate empty result into an error — the same defect in a new coat.

    Fails OPEN. When ``present_rooms()`` cannot tell, this passes.
    ``canonical_rooms()`` falls back to the spec's seven on the same
    failure, which is right for a write guard — refuse when unsure, the
    caller retries, nothing is lost — and wrong here, because the costs are
    asymmetric: a false pass returns an empty page, a false refusal makes
    existing data unreachable, which is the bug.
    """
    if room is None:
        return
    canonical = canonical_rooms()
    if room in canonical:
        return
    present = present_rooms()
    if present is None or room in present:
        return
    # A miss is the one case worth a second look: the cache may predate a
    # room that now exists, and refusing on stale data recreates the bug.
    # No `is None` guard here, deliberately: reaching this line means the
    # check above saw a populated cache, and a refresh whose read fails
    # falls back to that same cache — so it cannot be None. Writing the
    # guard anyway would look defensive and would in fact be dead code
    # that silently rescues a fail-closed regression above it.
    present = present_rooms(refresh=True)
    if room in present:
        return
    from fastapi import HTTPException
    raise HTTPException(
        status_code=400,
        detail={
            "error": f"room {room!r} holds no drawers and is not canonical",
            # What is actually queryable — not the canonical seven, which
            # today advertise a list excluding the room holding the most
            # drawers in the palace.
            "valid_rooms": sorted(canonical | present),
        },
    )


def wing_filter_dep(wing: "str | None" = None):
    """FastAPI dependency wrapping ``normalize_wing_filter``.

    Endpoints declare ``wing: str | None = Depends(rooms.wing_filter_dep)``
    and get the canonicalized value at request-parse time. Saves the
    one-line ``wing = rooms.normalize_wing_filter(wing)`` boilerplate
    that PRs #175/#177/#178 had to add at 11 sites — and prevents the
    next new wing-accepting endpoint from forgetting it (palace-daemon#179).

    Implementation: FastAPI sees the ``wing`` query parameter via the
    function signature, then runs this body to canonicalize before
    binding to the endpoint's ``wing`` parameter.
    """
    return normalize_wing_filter(wing)


def room_validator_dep(room: "str | None" = None):
    """FastAPI dependency wrapping ``validate_room_filter_or_raise``.

    Endpoints declare ``room: str | None = Depends(rooms.room_validator_dep)``
    and get either a usable room filter or an HTTP 400 with a valid_rooms
    list — never an invalid room reaching the handler body.

    This is the READ side. It wrapped ``validate_room_or_raise`` — the write
    guard — until #285, which is how /list, /search and /window came to
    refuse room filters for rooms that hold drawers (79,889 of them, 8.6%
    of the corpus). Writes keep the canonical-only rule; a filter accepts
    any room that exists.

    Companion to ``wing_filter_dep``; together they replace the inline
    validate/normalize calls at the 6 read endpoints and prevent future
    regressions (palace-daemon#179).
    """
    validate_room_filter_or_raise(room)
    return room


def normalize_wing_filter(wing):
    """Normalize a wing slug for use as a read filter.

    The write-side ``normalize_wing_slug`` returns "unknown" for empty
    input — correct because writes need a non-null wing. For *filters*,
    empty input means "no filter" (read all wings), so None is correct.
    This wrapper preserves that distinction:

      normalize_wing_filter(None)              → None  (no filter)
      normalize_wing_filter("")                → None
      normalize_wing_filter("Palace_Daemon")   → "palace_daemon"
      normalize_wing_filter("wing_palace")     → "palace"

    Used by every read endpoint that accepts ``wing`` as a query filter
    (/search, /list, /search/hybrid, /search/keyword, /search/age-fused,
    /search/fast). Pre-fix these endpoints passed the caller's wing
    string through unchanged, so a write that landed under
    ``palace_daemon`` (normalized from "Palace_Daemon") couldn't be
    retrieved by querying ``Palace_Daemon`` — same asymmetric contract
    as the PATCH /memory{room} bug (#174).
    """
    if not wing:
        return None
    normalized = normalize_wing_slug(wing)
    # normalize_wing_slug fall-back returns "unknown" for unparseable
    # input — that's not a valid filter, treat as "no filter."
    if normalized == "unknown":
        return None
    return normalized


def validate_room_or_raise(room):
    """Raise HTTP 400 if ``room`` is set and not canonical.

    Used by /search/hybrid and /search/age-fused for fast-feedback room
    validation (vs an empty-result silent surprise from a typo). Shared
    helper consolidates two near-identical inline blocks that had drifted
    apart on error-message text.

    Pass-through (no exception) when ``room`` is None or matches a
    canonical room name.
    """
    if room is None:
        return
    if room in canonical_rooms():
        return
    from fastapi import HTTPException
    raise HTTPException(
        status_code=400,
        detail={
            "error": f"room {room!r} is not in the canonical set",
            "valid_rooms": sorted(canonical_rooms()),
        },
    )
