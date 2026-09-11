"""Daemon-native MCP tools — extracted from main.py per #101 (fifth slice).

Owns the six `mempalace_*` tools that replaced the local-ChromaDB-opening
CLI commands in mempalace (`cmd_rooms`, `cmd_wakeup`, `cmd_mined`) — see
palace-daemon#93. The /mcp fast-intercept dispatcher in main.py looks up
the handler via ``DAEMON_NATIVE_TOOLS[name]``.

Module layout:

- ``normalize_room_name(raw)`` — string normalization shared across all
  rooms tools (lowercased, stripped, blank-rejected).
- ``invalidate_rooms_cache()`` — drops ``main._canonical_rooms_cache``
  via a lazy ``import main`` so we don't have a hard cycle at import time.
  Python resolves the attribute write at call time, after both modules
  are fully loaded, which is safe.
- ``fast_mcp_rooms_list / rooms_add / rooms_rename / rooms_remove`` —
  CRUD over ``mempalace_canonical_rooms``.
- ``fast_mcp_mined`` — groups drawer.metadata.source_file by wing.
- ``fast_mcp_wakeup`` — delegates to ``mempalace.layers.MemoryStack``.
- ``DAEMON_NATIVE_TOOLS`` — name → handler registry.

main.py re-exports these names under their original ``_``-prefixed form
so tests that ``main._fast_mcp_*`` keep working without edits.
"""
from __future__ import annotations

from postgres import (
    _DaemonToolError,
    _RPC_INTERNAL,
    _RPC_INVALID_PARAMS,
    connect_postgres,
)


def normalize_room_name(raw):
    """Match the CLI's normalization: stripped, lowercased, non-empty str."""
    if not isinstance(raw, str):
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "room name must be a string"
        )
    name = raw.strip().lower()
    if not name:
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "room name cannot be blank"
        )
    return name


def invalidate_rooms_cache():
    """Drop the in-process canonical-rooms cache; next read rebuilds it.

    Post-#101 twelfth slice the cache lives in ``rooms.py`` (not main).
    No lazy-import dance needed anymore — rooms.py and daemon_tools.py
    don't import each other, so the top-level import is safe.
    """
    import rooms
    rooms._canonical_rooms_cache = None


def fast_mcp_rooms_list(arguments: dict) -> list[dict]:
    """`mempalace_rooms_list({})` → list of {name, description, added_at}.

    Schema-not-deployed (UndefinedTable) returns [] rather than erroring,
    matching `cmd_rooms list`'s "(no canonical rooms registered)" UX.
    """
    from psycopg2 import errors as pg_errors
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                try:
                    cur.execute(
                        "SELECT name, COALESCE(description, '') AS description, added_at "
                        "FROM mempalace_canonical_rooms ORDER BY name"
                    )
                    rows = cur.fetchall()
                except pg_errors.UndefinedTable:
                    return []
    finally:
        conn.close()
    return [
        {"name": r[0], "description": r[1], "added_at": r[2]} for r in rows
    ]


def fast_mcp_rooms_add(arguments: dict) -> dict:
    """`mempalace_rooms_add({name, description?})` → {action, name}.

    `action` is "added" on INSERT, "updated" on the ON CONFLICT branch.
    Mirrors the CLI's `xmax=0` trick so the user-visible verbiage matches.
    """
    name = normalize_room_name(arguments.get("name"))
    description = arguments.get("description")
    if description is not None and not isinstance(description, str):
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "description must be a string or omitted"
        )
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                # xmax = 0 ⇒ the row was INSERTed; non-zero ⇒ UPDATEd.
                # Postgres's writeable-CTE trick: the system column is only
                # available on the row produced by the INSERT path.
                cur.execute(
                    "INSERT INTO mempalace_canonical_rooms (name, description, added_at) "
                    "VALUES (%s, %s, now()) "
                    "ON CONFLICT (name) DO UPDATE "
                    "  SET description = EXCLUDED.description "
                    "RETURNING (xmax = 0) AS inserted",
                    (name, description),
                )
                inserted = cur.fetchone()[0]
    finally:
        conn.close()
    invalidate_rooms_cache()
    return {"action": "added" if inserted else "updated", "name": name}


def fast_mcp_rooms_rename(arguments: dict) -> dict:
    """`mempalace_rooms_rename({old, new})` → {old, new, affected_drawers}.

    Relies on the `ON UPDATE CASCADE` FK on `mempalace_drawers.room` so the
    rename is a single statement that cascades to every referencing row.
    """
    old = normalize_room_name(arguments.get("old"))
    new = normalize_room_name(arguments.get("new"))
    if old == new:
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "old and new room names are identical"
        )
    from psycopg2 import errors as pg_errors
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '10s'")
                # Count first so we can report the cascade size — the count
                # is approximate (no advisory lock) but inside the same
                # transaction it's accurate enough for "drawers affected".
                cur.execute(
                    "SELECT count(*) FROM mempalace_drawers WHERE room = %s",
                    (old,),
                )
                affected = cur.fetchone()[0]
                try:
                    cur.execute(
                        "UPDATE mempalace_canonical_rooms SET name = %s WHERE name = %s",
                        (new, old),
                    )
                    if cur.rowcount == 0:
                        raise _DaemonToolError(
                            _RPC_INVALID_PARAMS,
                            f"room {old!r} does not exist",
                        )
                except pg_errors.UniqueViolation:
                    raise _DaemonToolError(
                        _RPC_INVALID_PARAMS,
                        f"target room {new!r} already exists",
                    )
    finally:
        conn.close()
    invalidate_rooms_cache()
    return {"old": old, "new": new, "affected_drawers": int(affected)}


def fast_mcp_rooms_remove(arguments: dict) -> dict:
    """`mempalace_rooms_remove({name})` → {name, removed: bool}.

    Refuses with -32602 + drawer count if any drawer still references the
    room — the operator should `mempalace purge --room <name>` or rename
    first. The count makes the refusal actionable rather than blunt.
    """
    name = normalize_room_name(arguments.get("name"))
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                cur.execute(
                    "SELECT count(*) FROM mempalace_drawers WHERE room = %s",
                    (name,),
                )
                referencing = cur.fetchone()[0]
                if referencing > 0:
                    raise _DaemonToolError(
                        _RPC_INVALID_PARAMS,
                        f"cannot remove {name!r}: {referencing} drawers still reference it",
                        data={"name": name, "referencing_drawers": referencing},
                    )
                cur.execute(
                    "DELETE FROM mempalace_canonical_rooms WHERE name = %s",
                    (name,),
                )
                removed = cur.rowcount > 0
    finally:
        conn.close()
    invalidate_rooms_cache()
    return {"name": name, "removed": bool(removed)}


def fast_mcp_mined(arguments: dict) -> dict:
    """`mempalace_mined({wing?, limit?})` → grouped sources by wing.

    Walks ``mempalace_drawers.metadata`` for `source_file` and groups by
    wing. Skips drawers whose metadata lacks the key entirely OR has a
    blank source_file (diary entries / kg drawers / manual additions).

    Each source also carries ``max_source_mtime``: the newest
    ``source_mtime`` recorded across that source's drawers — the file's mtime
    as the miner saw it. That is what makes "has this file moved on since we
    indexed it?" answerable in ONE request, which the caller then decides
    with ``mempalace.provenance.source_stale`` rather than a second notion of
    staleness.

    It had to go here rather than in a new route: this aggregation already
    performs exactly the required ``GROUP BY wing, source_file``, and a
    second endpoint would have been a second copy of it. The alternative
    sources were measured and neither works — ``/search/fast`` carries
    ``source_mtime`` but is BM25-ranked, so "not in the top N" is
    indistinguishable from "not indexed" and a check built on it fails toward
    a clean bill of health; ``/search/keyword`` costs 2.6s and its
    ``source_indexed_at`` was ``None`` on every sampled hit.

    ``None`` means no drawer for that source recorded an mtime, and callers
    MUST read it as unknown — never as 0, and never as fresh.
    """
    wing_filter = arguments.get("wing")
    if wing_filter is not None and not isinstance(wing_filter, str):
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "wing must be a string or omitted"
        )
    limit = arguments.get("limit")
    if limit is not None:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise _DaemonToolError(
                _RPC_INVALID_PARAMS, "limit must be an integer or omitted"
            )
        if limit <= 0:
            raise _DaemonToolError(
                _RPC_INVALID_PARAMS, "limit must be positive"
            )
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '10s'")
                # The mtime cast is guarded rather than written as a bare
                # ``::double precision``: one malformed value in one drawer
                # would otherwise abort the whole aggregation and take the
                # check out for every file in the wing. A bad value becomes
                # NULL, which max() ignores.
                #
                # The regex alone is not enough. It rejects 'NaN' and
                # 'Infinity' (verified in Postgres: both would otherwise cast
                # fine and produce invalid JSON), but a long run of digits
                # passes it and OVERFLOWS the cast — measured, a 400-digit
                # integer raises "value out of range". The length bound is
                # what closes that: a real mtime is ~10 digits plus a
                # fractional part, so 20 characters is far past any true
                # value and far short of the ~308 where double precision
                # gives out.
                sql = (
                    "SELECT wing, metadata->>'source_file' AS source_file, count(*) AS n, "
                    "max(CASE WHEN metadata->>'source_mtime' ~ "
                    "'^[0-9]+(\\.[0-9]+)?$' "
                    "AND length(metadata->>'source_mtime') <= 20 "
                    "THEN (metadata->>'source_mtime')::double precision END) AS max_mtime "
                    "FROM mempalace_drawers "
                    "WHERE metadata ? 'source_file' "
                    "  AND metadata->>'source_file' <> '' "
                )
                params_list: list = []
                if wing_filter:
                    sql += "AND wing = %s "
                    params_list.append(wing_filter)
                sql += "GROUP BY wing, source_file ORDER BY wing, n DESC"
                cur.execute(sql, params_list)
                rows = cur.fetchall()
    finally:
        conn.close()
    # Group into the issue's shape, honouring per-wing limit.
    by_wing: dict[str, dict] = {}
    for wing, source_file, n, max_mtime in rows:
        slot = by_wing.setdefault(
            wing, {"sources": [], "total_sources": 0, "total_drawers": 0, "truncated": False}
        )
        if limit is None or slot["total_sources"] < limit:
            slot["sources"].append(
                {
                    "source_file": source_file,
                    "drawer_count": int(n),
                    "max_source_mtime": float(max_mtime) if max_mtime is not None else None,
                }
            )
        else:
            slot["truncated"] = True
        slot["total_sources"] += 1
        slot["total_drawers"] += int(n)
    total_sources = sum(slot["total_sources"] for slot in by_wing.values())
    return {
        "sources_by_wing": by_wing,
        "wing_filter": wing_filter,
        "total_wings": len(by_wing),
        "total_sources": total_sources,
    }


def fast_mcp_wakeup(arguments: dict) -> dict:
    """`mempalace_wakeup({wing?})` → {text, tokens}.

    Delegates to ``mempalace.layers.MemoryStack().wake_up(wing=...)`` so
    the L0 (identity) + L1 (essential story) rendering stays in one place
    and the daemon doesn't drift from the CLI's output shape. MemoryStack
    reads ``mempalace.config.MempalaceConfig`` for its palace path and
    talks to whichever backend is configured (chroma / postgres) — the
    daemon's editable mempalace install makes this transparent.
    """
    wing = arguments.get("wing")
    if wing is not None and not isinstance(wing, str):
        raise _DaemonToolError(
            _RPC_INVALID_PARAMS, "wing must be a string or omitted"
        )
    try:
        from mempalace.layers import MemoryStack
        stack = MemoryStack()
        text = stack.wake_up(wing=wing)
    except Exception as e:
        # Bubble up as internal so the operator can see the cause; the
        # MemoryStack path can fail for many reasons (config missing,
        # identity file unreadable, backend down) and the message is the
        # most useful signal for triage.
        raise _DaemonToolError(
            _RPC_INTERNAL, f"wake_up failed: {e}"
        )
    tokens = len(text) // 4  # match the CLI's rough estimate
    return {"text": text, "tokens": tokens, "wing": wing}



# ─────────────────────────────────────────────────────────────────────────────
# Hallways (#255) — answerable only because mempalace#442 gave them a table
# ─────────────────────────────────────────────────────────────────────────────

# 797K records across 21 wings on the production palace. Any default that
# could return the store reproduces the hazard this tool was filed about, so
# the page size is bounded here and a caller cannot raise it past the cap.
HALLWAY_DEFAULT_LIMIT = 200
HALLWAY_MAX_LIMIT = 2000

_HALLWAY_COLUMNS = (
    "id, wing, entity_a, entity_b, co_occurrence_count, rooms, label, "
    "created_at, created_by, dynamics, extra"
)


def _hallway_row_to_record(row):
    """Reassemble a hallway record from its promoted columns + jsonb blobs.

    Mirrors ``mempalace.hallway_store._row_to_record``. Kept as a small local
    function rather than importing mempalace: the daemon answers this without
    loading the mempalace package at all, which is the point of a fast
    intercept.
    """
    record = {
        "id": row[0],
        "wing": row[1],
        "entity_a": row[2],
        "entity_b": row[3],
        "co_occurrence_count": row[4],
        "rooms": list(row[5] or []),
        "label": row[6],
        "created_at": row[7],
        "created_by": row[8],
    }
    for blob in (row[9], row[10]):
        if isinstance(blob, dict):
            record.update(blob)
    return {k: v for k, v in record.items() if not (k == "label" and v is None)}


def fast_mcp_list_hallways(arguments: dict) -> dict:
    """`mempalace_list_hallways({wing?, limit?, offset?})` → bounded page.

    Before mempalace#442 this could not be fast-intercepted at all: the store
    was a 1.04 GB JSON file, ``list_hallways`` loaded all of it per call, and
    the ``wing`` argument did not reduce the work. Parsing that array
    materializes several GB against this daemon's 2 GB cgroup cap, so the
    call was a memory hazard rather than merely a slow one.

    With the table, the wing filter and the page are both SQL.
    """
    arguments = arguments or {}
    wing = arguments.get("wing")
    if wing is not None and not isinstance(wing, str):
        raise _DaemonToolError(_RPC_INVALID_PARAMS, "wing must be a string")

    try:
        limit = int(arguments.get("limit") or HALLWAY_DEFAULT_LIMIT)
    except (TypeError, ValueError):
        raise _DaemonToolError(_RPC_INVALID_PARAMS, "limit must be an integer") from None
    try:
        offset = int(arguments.get("offset") or 0)
    except (TypeError, ValueError):
        raise _DaemonToolError(_RPC_INVALID_PARAMS, "offset must be an integer") from None
    limit = max(1, min(limit, HALLWAY_MAX_LIMIT))
    offset = max(0, offset)

    sql = f"SELECT {_HALLWAY_COLUMNS} FROM mempalace_hallways"
    params = []
    if wing is not None:
        sql += " WHERE wing = %s"
        params.append(wing)
    sql += " ORDER BY co_occurrence_count DESC, id"
    # Fetch one extra row so "there are more" is a fact rather than a guess:
    # a page that happens to be exactly full is otherwise indistinguishable
    # from the end of the list, and a silently truncated answer reads as
    # "that is all of them".
    sql += " LIMIT %s OFFSET %s" if offset else " LIMIT %s"
    params.append(limit + 1)
    if offset:
        params.append(offset)

    from psycopg2 import errors as pg_errors
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '5s'")
                try:
                    cur.execute(sql, params)
                    rows = cur.fetchall()
                except pg_errors.UndefinedTable:
                    # The table only exists once mempalace#442's migration has
                    # run. A bare [] here is indistinguishable from "this
                    # palace has no hallways", which would send an operator
                    # looking in entirely the wrong place.
                    return {
                        "hallways": [],
                        "wing": wing,
                        "limit": limit,
                        "offset": offset,
                        "truncated": False,
                        "note": (
                            "mempalace_hallways table does not exist yet; run "
                            "python -m mempalace.migrate_hallways to import "
                            "hallways.json, then set hallway_backend=postgres"
                        ),
                    }
    finally:
        conn.close()

    truncated = len(rows) > limit
    records = [_hallway_row_to_record(r) for r in rows[:limit]]
    return {
        "hallways": records,
        "wing": wing,
        "limit": limit,
        "offset": offset,
        "truncated": truncated,
    }


DAEMON_NATIVE_TOOLS = {
    "mempalace_rooms_list": fast_mcp_rooms_list,
    "mempalace_rooms_add": fast_mcp_rooms_add,
    "mempalace_rooms_rename": fast_mcp_rooms_rename,
    "mempalace_rooms_remove": fast_mcp_rooms_remove,
    "mempalace_mined": fast_mcp_mined,
    "mempalace_wakeup": fast_mcp_wakeup,
    "mempalace_list_hallways": fast_mcp_list_hallways,
}


# MCP tool descriptors for /mcp tools/list (#140). The /mcp proxy in main.py
# augments the upstream mempalace tools/list response with these so MCP
# clients (Claude Code, Claude Desktop, anyone using the standard
# discovery handshake) can find them. Without this, the tools are
# callable but invisible to discovery — consumers would have to hardcode
# the names, which defeats the protocol.
DAEMON_NATIVE_TOOL_DESCRIPTORS = [
    {
        "name": "mempalace_list_hallways",
        "description": (
            "List within-wing entity-to-entity hallways from the "
            "mempalace_hallways table, strongest first. Bounded: pass wing "
            "to scope the query (the filter is SQL, so a scoped call scans "
            "only that wing), and limit/offset to page. `truncated` says "
            "whether more rows exist beyond this page. Returns an empty "
            "list with a note if the table has not been migrated yet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "restrict to one wing"},
                "limit": {
                    "type": "integer",
                    "description": f"page size (default {HALLWAY_DEFAULT_LIMIT}, max {HALLWAY_MAX_LIMIT})",
                },
                "offset": {"type": "integer", "description": "rows to skip"},
            },
        },
    },
    {
        "name": "mempalace_rooms_list",
        "description": (
            "List canonical rooms registered in the palace. Returns "
            "name + description + added_at for each row in "
            "mempalace_canonical_rooms. Empty list if the schema isn't "
            "deployed yet."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mempalace_rooms_add",
        "description": (
            "Register a canonical room (or update its description). "
            "Returns {action: 'added'|'updated', name}. Name is "
            "lowercased and stripped; blank names are rejected."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Room name (case-insensitive)."},
                "description": {"type": "string", "description": "Optional human-readable description."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "mempalace_rooms_rename",
        "description": (
            "Rename a canonical room. Cascades to mempalace_drawers.room "
            "via the FK's ON UPDATE CASCADE. Returns the count of "
            "affected drawers."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "old": {"type": "string", "description": "Current room name."},
                "new": {"type": "string", "description": "New room name."},
            },
            "required": ["old", "new"],
        },
    },
    {
        "name": "mempalace_rooms_remove",
        "description": (
            "Remove a canonical room. Refuses with the referencing drawer "
            "count if any drawer still uses the room — purge or rename "
            "those first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Room name to remove."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "mempalace_mined",
        "description": (
            "List source files that have been mined into drawers, grouped "
            "by wing. Skips drawers with no source_file metadata "
            "(diary, KG, manual additions). Returns "
            "{sources_by_wing, total_wings, total_sources}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Optional: restrict to one wing."},
                "limit": {"type": "integer", "description": "Optional: cap sources reported per wing."},
            },
        },
    },
    {
        "name": "mempalace_wakeup",
        "description": (
            "Render the L0 (identity) + L1 (essential story) wake-up "
            "context. Delegates to mempalace.layers.MemoryStack.wake_up. "
            "Returns {text, tokens, wing}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "wing": {"type": "string", "description": "Optional: scope to one wing."},
            },
        },
    },
]
