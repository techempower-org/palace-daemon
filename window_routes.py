"""``/window`` and ``/source`` — time-ordered and by-file drawer listings.

mempalace#500 wants "what happened between these two timestamps", and
#502 wants "the drawers from THAT file, in order". Neither exists today:
``search --since`` *filters a ranked* search, so inside the window you get
whatever scores highest (a 2g session kept landing on the same three
high-scoring drawers instead of the sequence), and ``/list`` is
insertion-ordered with no time filter at all — against a 201K-drawer wing
a date is ~200 pages away.

**What these routes change, precisely: the performance envelope, not the
semantics.** The time contract already exists and is deliberate:

- ``mempalace/date_window.parse_window`` — *"any timezone offset on the
  input is dropped… comparison is therefore wall-clock"*, used by
  ``searcher.py``, ``provenance.py`` and ``mcp_server.py``;
- ``tool_list_drawers`` — ``since`` inclusive, ``before`` exclusive
  (#1128), and a drawer whose ``filed_at`` is missing is excluded while a
  bound is active.

Those bounds are parsed here by *calling that helper*, not by a second
parser, so ``/window`` cannot drift from ``list --since``. What was slow
is that the filter ran in Python after fetching every row — a ChromaDB-era
constraint. In postgres it is a predicate, which is the whole win.

The remaining correctness problem is upstream of these routes and is filed
separately: ``filed_at`` holds two timezone conventions (915,562 naive
host-local, 5,474 ``Z``/UTC), so a wall-clock walk places 0.6 % of drawers
up to 7 h out of position (mempalace#506). ``window_query`` keeps the
ordering key in one function so that fix is a small diff here.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query

import rooms as _rooms
import window_query as wq
from postgres import connect_postgres

try:  # the daemon runs alongside mempalace; the helper is the contract
    from mempalace.date_window import parse_window
except ImportError:  # pragma: no cover - mempalace always present in deploy

    def parse_window(since=None, before=None):  # type: ignore[misc]
        raise RuntimeError(
            "mempalace.date_window is unavailable; /window refuses to parse "
            "bounds with a second implementation because it would drift from "
            "list --since"
        )


router = APIRouter()


def _envelope(row) -> dict:
    """One drawer, in the shape the CLI renderers already understand."""
    from pathlib import Path

    drawer_id, wing, room, document, metadata, filed_at = row
    meta = dict(metadata or {})
    meta["wing"] = wing or ""
    meta["room"] = room or ""
    if meta.get("source_file"):
        meta["source_file"] = Path(str(meta["source_file"])).name
    doc = document or ""
    return {
        "drawer_id": drawer_id,
        "wing": wing or "",
        "room": room or "",
        "filed_at": filed_at,
        "content_preview": doc[:200] + "..." if len(doc) > 200 else doc,
        "metadata": meta,
    }


def _run(sql: str, params: list) -> list:
    conn = connect_postgres()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '30s'")
                cur.execute(sql, params)
                return cur.fetchall()
    finally:
        conn.close()


@router.get("/window")
async def window(
    wing: str | None = Depends(_rooms.wing_filter_dep),
    room: str | None = Depends(_rooms.room_validator_dep),
    since: Optional[str] = None,
    before: Optional[str] = None,
    source_file: Optional[str] = None,
    limit: int = 100,
    cursor: Optional[str] = None,
    x_api_key: str | None = Header(default=None),
):
    """Drawers in filed order between two bounds. Unranked, keyset-paged.

    ``since`` inclusive, ``before`` exclusive, wall-clock — the existing
    contract, parsed by ``mempalace.date_window.parse_window`` rather than
    reimplemented. Drawers with no ``filed_at`` are excluded while a bound
    is active, and the count of them is reported in
    ``excluded_no_filed_at`` so the exclusion is visible rather than silent.
    """
    import main as _main

    _main._check_auth(x_api_key)

    # Bounds first: a 400 here must not depend on reaching the database.
    try:
        parse_window(since, before)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        sql, params = wq.build_window_sql(
            wing=wing,
            room=room,
            since=since,
            before=before,
            source_file=source_file,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as exc:  # corrupt cursor, bad ordering/zone config
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = _run(sql, params)
    drawers = [_envelope(r) for r in rows]

    effective_limit = max(1, min(int(limit), wq.MAX_PAGE))
    next_cursor = None
    if len(rows) == effective_limit and rows:
        next_cursor = wq.encode_cursor(rows[-1][5], rows[-1][0])

    excluded = 0
    if since or before:
        csql, cparams = wq.count_missing_filed_at_sql(wing=wing)
        found = _run(csql, cparams)
        excluded = int(found[0][0]) if found else 0

    return {
        "drawers": drawers,
        "count": len(drawers),
        "wing": wing,
        "room": room,
        "since": since,
        "before": before,
        "limit": effective_limit,
        "next_cursor": next_cursor,
        # Which time semantics answered this. The contract may move to
        # normalised instants (mempalace#506); a caller must be able to tell
        # which answer it got rather than infer it from the version.
        "ordering": wq.DEFAULT_ORDERING,
        "excluded_no_filed_at": excluded,
    }


@router.get("/source")
async def source(
    source_file: str = Query(...),
    wing: str | None = Depends(_rooms.wing_filter_dep),
    limit: int = wq.MAX_PAGE,
    x_api_key: str | None = Header(default=None),
):
    """Every drawer from one source file, in chunk order (mempalace#502).

    Chunk order, not time order: ``chunk_index`` cast to int so chunk 10
    follows chunk 2 rather than preceding it, then ``filed_at``, then id.
    A drawer with no ``chunk_index`` (an unchunked single from the same
    file) sorts last rather than being dropped.
    """
    import main as _main

    _main._check_auth(x_api_key)

    if not source_file or not source_file.strip():
        raise HTTPException(status_code=400, detail="source_file must not be blank")

    try:
        sql, params = wq.build_source_sql(
            source_file=source_file.strip(), wing=wing, limit=limit
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    rows = _run(sql, params)
    return {
        "drawers": [_envelope(r) for r in rows],
        "count": len(rows),
        "source_file": source_file.strip(),
        "wing": wing,
    }
