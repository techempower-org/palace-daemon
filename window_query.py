"""SQL for the time-ordered and by-source drawer listings (mempalace#500/#502).

Two facts read off the production palace on 2026-09-17 shape this module.

**There is no `created_at` column.** The live `mempalace_drawers` is
``id | document | embedding | metadata | wing | room | doc_tsv``, and the
only indexes are on ``id``, ``wing``, ``room``, ``metadata`` (GIN,
containment — useless for a range), ``doc_tsv``, ``document`` and
``embedding``. Time lives in ``metadata->>'filed_at'``. (#500's text says
"ordering by ``created_at`` — pgvector backend already indexes it"; that
describes upstream's ``pgvector.py`` table, which carries
``updated_at timestamptz``. The fork's ``postgres.py`` shape that
production runs has neither.)

**``filed_at`` carries two timezone conventions.** Whole-table shapes:

===========================  =======  ==========================
shape                        rows     convention
===========================  =======  ==========================
``9999-99-99T99:99:99.999999``  915,562  naive, palace-host LOCAL
``9999-99-99T99:99:99.999Z``      5,474  UTC
``9999-99-99T99:99:99``              34  naive, no fraction
(key absent)                      2,613  no time at all
===========================  =======  ==========================

A text sort and a ``::timestamp`` sort agree perfectly — 0 rank
differences over 60,000 rows on two different wings — because postgres
**discards** the ``Z`` (``'…372Z'::timestamp`` → ``…372``, no offset
applied). They agree because they are wrong in the same way, so that
agreement is not reassurance. Tracked as mempalace#506.

The ordering key is therefore pluggable, and both modes are exercised by
the tests:

``wallclock``
    Compare the stored string directly. Matches the contract already
    documented in ``mempalace/date_window.py`` — *"any timezone offset on
    the input is dropped… comparison is therefore wall-clock"* — which
    ``searcher.py``, ``provenance.py`` and ``mcp_server.py`` all use. A
    listing that disagreed with ``list --since`` about the same drawers
    would be worse than one that is wrong in a documented way, so this is
    the default until #506 settles the question for every surface at once.
``instant``
    Normalise to a real instant: ``Z`` → UTC, naive → the palace host's
    zone *by name* (so DST is handled; a fixed offset is wrong half the
    year). Chronologically correct, and inconsistent with the other
    surfaces until they move too.

Neither mode ranks, and neither touches the embedding column.
"""

from __future__ import annotations

import base64
import binascii
from typing import Optional

TABLE = "mempalace_drawers"

#: Page cap. #500 asks for a walk, not a bulk export; ``/list`` caps at 100
#: and the MCP tool at 100, so 1000 is already an order of magnitude more
#: generous while still bounding one response.
MAX_PAGE = 1000

#: Conservative until mempalace#506 decides for every date-filtered surface.
DEFAULT_ORDERING = "wallclock"

#: How far the coarse text prefilter reaches beyond the exact bound in
#: ``instant`` mode. Real offsets span UTC-12..UTC+14, so one day covers
#: every zone with room to spare; the exact predicate then does the real
#: filtering over a small candidate set.
_PREFILTER_SLACK_DAYS = 1

_FILED_AT = f"({TABLE[0]}.metadata->>'filed_at')".replace(f"{TABLE[0]}.", "d.")


def _valid_zone(name: str) -> bool:
    """True when ``name`` is a real IANA zone.

    Checked rather than trusted because the zone reaches SQL as a literal:
    it comes from configuration, and configuration is an input.
    """
    if not name or not isinstance(name, str):
        return False
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False


def instant_expr(ordering: str = DEFAULT_ORDERING, tz_name: str = "UTC") -> str:
    """Return the SQL expression this listing orders and filters on.

    ``wallclock`` is the stored text. ``instant`` is a real instant, with
    the ``Z`` rows read as UTC and the naive majority read in ``tz_name``.
    """
    if ordering == "wallclock":
        return _FILED_AT
    if ordering != "instant":
        raise ValueError(
            f"unknown ordering {ordering!r}; expected 'wallclock' or 'instant'"
        )
    if not _valid_zone(tz_name):
        raise ValueError(
            f"filed_at timezone {tz_name!r} is not a known IANA zone name "
            "(e.g. 'America/Los_Angeles'); a fixed offset is not accepted "
            "because it is wrong for half the year"
        )
    # The Z rows are UTC; everything else is the palace host's wall clock.
    # Only the final character is inspected, so a value that merely contains
    # a 'Z' elsewhere is handled by the ELSE branch.
    #
    # `right(x, 1) = 'Z'` rather than `LIKE '%Z'` on purpose: psycopg parses
    # `%Z` in query TEXT as a placeholder and raises
    # "only '%s', '%b', '%t' are allowed as placeholders, got '%Z'".
    # Escaping it as `%%Z` would work and would be one forgotten doubling
    # away from breaking again, so the expression avoids `%` entirely.
    # Caught by the live ordering test; no SQL-text assertion could see it.
    return (
        "CASE WHEN "
        f"right({_FILED_AT}, 1) = 'Z' "
        f"THEN (left({_FILED_AT}, length({_FILED_AT}) - 1))::timestamp "
        "AT TIME ZONE 'UTC' "
        f"ELSE {_FILED_AT}::timestamp AT TIME ZONE '{tz_name}' "
        "END"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Keyset cursor
# ─────────────────────────────────────────────────────────────────────────────

_SEP = "\x1f"  # unit separator: cannot occur in an ISO timestamp or a drawer id


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def encode_cursor(filed_at: str, drawer_id: str) -> str:
    """Opaque token for the last row of a page.

    Opaque on purpose: a readable ``"<ts>|<id>"`` invites callers to build
    one by hand, and then the separator choice becomes a compatibility
    promise. The separator here is US (0x1f), which cannot appear in an ISO
    timestamp or a drawer id, so an id containing ``|`` or ``:`` round-trips.
    """
    return _b64(f"{filed_at}{_SEP}{drawer_id}")


def decode_cursor(token: str) -> tuple[str, str]:
    """Inverse of :func:`encode_cursor`. Raises ``ValueError`` on anything else.

    Raising matters: a cursor that silently decoded to "start from the
    beginning" would repeat rows the caller already has, and a page walk
    that quietly restarts looks like data rather than an error.
    """
    if not token or not isinstance(token, str):
        raise ValueError("cursor must be a non-empty string")
    pad = "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"cursor is not a valid token: {token!r}") from exc
    if _SEP not in raw:
        raise ValueError(f"cursor is malformed (no field separator): {token!r}")
    filed_at, drawer_id = raw.split(_SEP, 1)
    if not filed_at or not drawer_id:
        raise ValueError(f"cursor is missing a field: {token!r}")
    return filed_at, drawer_id


# ─────────────────────────────────────────────────────────────────────────────
# Queries
# ─────────────────────────────────────────────────────────────────────────────

_SELECT = (
    "SELECT d.id, d.wing, d.room, d.document, d.metadata, "
    f"{_FILED_AT} AS filed_at"
)


def _shift_iso_day(bound: str, days: int) -> str:
    """Widen a bound by whole days, textually, without parsing precision away."""
    from datetime import datetime, timedelta

    text = bound[:-1] if bound.endswith(("Z", "z")) else bound
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        # Not our job to validate here — the route parses bounds first and
        # returns 400. If it somehow reaches us unparseable, don't widen.
        return bound
    return (parsed + timedelta(days=days)).isoformat()


def build_window_sql(
    *,
    wing: Optional[str] = None,
    room: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    source_file: Optional[str] = None,
    cursor: Optional[str] = None,
    limit: int = 100,
    ordering: str = DEFAULT_ORDERING,
    tz_name: str = "UTC",
) -> tuple[str, list]:
    """Build the time-ordered page query. Returns ``(sql, params)``.

    ``since`` is inclusive and ``before`` exclusive, matching
    ``tool_list_drawers``' documented ``[since, before)`` (#1128). A drawer
    whose ``filed_at`` is absent is excluded whenever a bound is active —
    also the existing contract, and 2,613 rows in production.
    """
    key = instant_expr(ordering, tz_name)
    where: list[str] = []
    params: list = []

    if wing:
        where.append("d.wing = %s")
        params.append(wing)
    if room:
        where.append("d.room = %s")
        params.append(room)
    if source_file:
        # Stored values are absolute paths; callers paste what search showed
        # them, which is the basename. Match either without a leading-wildcard
        # LIKE on the whole column.
        where.append(
            f"(d.metadata->>'source_file' = %s "
            f"OR d.metadata->>'source_file' LIKE %s)"
        )
        params.extend([source_file, f"%/{source_file.lstrip('/')}"])

    if since or before:
        where.append(f"{_FILED_AT} IS NOT NULL")

    # Coarse, index-friendly prefilter on the raw text. Only needed in
    # ``instant`` mode, where the exact predicate is on a normalised value
    # the planner cannot use an index for: widen by a day so a Z row near
    # the edge is still a candidate when normalisation moves it.
    if ordering == "instant":
        if since:
            where.append(f"{_FILED_AT} >= %s")
            params.append(_shift_iso_day(since, -_PREFILTER_SLACK_DAYS))
        if before:
            where.append(f"{_FILED_AT} < %s")
            params.append(_shift_iso_day(before, _PREFILTER_SLACK_DAYS))

    if since:
        where.append(f"{key} >= %s")
        params.append(since)
    if before:
        where.append(f"{key} < %s")
        params.append(before)

    if cursor:
        c_filed, c_id = decode_cursor(cursor)
        # Strict tuple comparison, not ``key > c_filed``: four drawers in the
        # production sample shared one filed_at to the microsecond, and a
        # scalar comparison would skip every row on the boundary timestamp.
        where.append(f"({key}, d.id) > (%s, %s)")
        params.extend([c_filed, c_id])

    sql = f"{_SELECT} FROM {TABLE} d"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {key} ASC, d.id ASC LIMIT %s"
    params.append(max(1, min(int(limit), MAX_PAGE)))
    return sql, params


def build_source_sql(
    *,
    source_file: str,
    wing: Optional[str] = None,
    limit: int = MAX_PAGE,
) -> tuple[str, list]:
    """Build the by-source-file query, in chunk order (mempalace#502).

    Ordered by ``chunk_index`` **numerically** — lexically, chunk 10 sorts
    before chunk 2, which is precisely the "in order" the issue is asking
    for. Drawers without a ``chunk_index`` (singles) sort last within the
    file rather than being dropped.
    """
    if not source_file:
        raise ValueError("source_file is required")
    where = [
        "(d.metadata->>'source_file' = %s OR d.metadata->>'source_file' LIKE %s)"
    ]
    params: list = [source_file, f"%/{source_file.lstrip('/')}"]
    if wing:
        where.append("d.wing = %s")
        params.append(wing)

    # NULLS LAST so a single (unchunked) drawer from the same file appears
    # after the chunk sequence instead of leading it.
    chunk = (
        "CASE WHEN d.metadata->>'chunk_index' ~ '^[0-9]+$' "
        "THEN (d.metadata->>'chunk_index')::int END"
    )
    sql = (
        f"{_SELECT} FROM {TABLE} d WHERE " + " AND ".join(where)
        + f" ORDER BY {chunk} ASC NULLS LAST, {_FILED_AT} ASC, d.id ASC LIMIT %s"
    )
    params.append(max(1, min(int(limit), MAX_PAGE)))
    return sql, params


def count_missing_filed_at_sql(*, wing: Optional[str] = None) -> tuple[str, list]:
    """Count drawers a time window can never return, so the caller is told.

    2,613 in production (0.283%). The existing contract excludes them under
    a bound; reporting the number is the difference between an exclusion and
    a disappearance.
    """
    sql = f"SELECT count(*) FROM {TABLE} d WHERE {_FILED_AT} IS NULL"
    params: list = []
    if wing:
        sql += " AND d.wing = %s"
        params.append(wing)
    return sql, params
