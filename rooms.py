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
    except Exception:
        _canonical_rooms_cache = DEFAULTS
    return _canonical_rooms_cache
