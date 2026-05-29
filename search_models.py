"""Pydantic body models for the POST /search/* endpoints.

Closes the second half of palace-daemon#179. The query-param read
endpoints (GET /search, /list, /search/fast) already use FastAPI
dependencies (rooms.wing_filter_dep, rooms.room_validator_dep) — PR
#180. POST endpoints need pydantic models because their wing/room
fields come from the parsed JSON body, not query strings.

Each model has field validators that route wing through
``rooms.normalize_wing_filter`` and room through
``rooms.validate_room_or_raise``. The validators run at request-parse
time so handler bodies receive already-canonicalized values — same
contract as the dependency-using endpoints.

This module is the durable structural answer to the asymmetric-
canonicalization bug class (#174 PATCH room, #175 read wing, #177
silent-save wing, #178 /mine + /backfill-age + watcher wing) — when
all endpoints declare their inputs via these models, a new endpoint
can't silently bypass the contract.
"""
from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

import rooms


# Field validator helpers — shared so the three models agree on the
# canonical shape.

def _canon_wing(value):
    """Pydantic field validator: normalize wing per write/read symmetry."""
    return rooms.normalize_wing_filter(value)


def _canon_room(value):
    """Pydantic field validator: validate room, raise HTTPException on bad."""
    # rooms.validate_room_or_raise returns None on None/canonical, raises
    # 400 on non-canonical. Return the normalized value (which is just the
    # input since rooms are not case-folded — only validated).
    rooms.validate_room_or_raise(value)
    return value


class SearchKeywordBody(BaseModel):
    """Body for POST /search/keyword."""

    query: str = Field(..., min_length=1, description="Required, non-empty search query.")
    wing: "str | None" = Field(None, description="Optional wing filter (canonicalized).")
    room: "str | None" = Field(None, description="Optional room filter (must be canonical if set).")
    limit: int = Field(20, ge=1, le=200, description="Result count (1..200).")

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v):
        v = (v or "").strip()
        if not v:
            # pydantic min_length doesn't strip; enforce here so the
            # daemon's contract matches the old inline behavior exactly.
            raise ValueError("'query' is required and must be non-empty")
        return v

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        return _canon_wing(v)

    @field_validator("room")
    @classmethod
    def _validate_room(cls, v):
        return _canon_room(v)


class SearchHybridBody(BaseModel):
    """Body for POST /search/hybrid."""

    query: str = Field(..., min_length=1)
    wing: "str | None" = Field(None)
    room: "str | None" = Field(None)
    limit: int = Field(10, ge=1, le=100)
    include_trace: bool = Field(False)
    # fusion_mode (#105): pass-through to mempalace's search_memories.
    # Forward-compat with mempalace#298 + #310. Validated to one of
    # 'convex' / 'rrf' to match mempalace's enum.
    fusion_mode: "str | None" = Field(None)
    # candidate_strategy (#80): hybrid candidate-strategy ablation.
    candidate_strategy: "str | None" = Field(None)
    # search_endpoint: alternate routing mode for /search/age-fused.
    # Kept here for the SME adapter's bench tooling.
    search_endpoint: "str | None" = Field(None)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("'query' is required and must be non-empty")
        return v

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        return _canon_wing(v)

    @field_validator("room")
    @classmethod
    def _validate_room(cls, v):
        return _canon_room(v)

    @field_validator("fusion_mode")
    @classmethod
    def _validate_fusion_mode(cls, v):
        if v is None:
            return v
        if not isinstance(v, str) or v not in ("convex", "rrf"):
            raise ValueError("'fusion_mode' must be 'convex' or 'rrf'")
        return v


class SilentSaveBody(BaseModel):
    """Body for POST /silent-save (Stop-hook diary checkpoint write).

    Differs from POST /memory's body model: empty wing stays as ``""``
    rather than coercing to ``"unknown"`` — the handler warns on empty
    in the themed systemMessage rather than synthesizing a default. This
    preserves the existing behavioral contract that hook clients may
    legitimately call /silent-save with no wing (e.g. before a workspace
    is assigned).

    Topic canonicalization stays in the handler via ``_canonical_topic``
    — it's a synonym-rewrite (e.g. "checkpoint" → CHECKPOINT_TOPIC) with
    a warning log on rewrite, semantically too involved for a pydantic
    validator to handle cleanly.
    """

    entry: str = Field(..., min_length=1, description="Diary entry body (required).")
    wing: str = Field("", description="Optional wing slug — normalized if set, empty allowed.")
    topic: "str | None" = Field(None, description="Optional topic — canonicalized in handler.")
    agent_name: str = Field("session-hook", description="Diary author name.")
    themes: "list | None" = Field(None, description="Optional theme tags for the systemMessage.")
    message_count: "int | None" = Field(None, ge=1, description="Conversation-turn count the hook displays.")
    session_id: "str | None" = Field(None, description="Optional session identifier.")

    @field_validator("entry")
    @classmethod
    def _require_entry(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("'entry' is required")
        return v

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        # /silent-save has WRITE-side wing semantics, not filter semantics —
        # we normalize via the write helper (matching /memory POST and #177)
        # rather than the filter helper that returns None on empty.
        from rooms import normalize_wing_slug
        if not v or not v.strip():
            return ""  # preserve empty so handler can warn
        return normalize_wing_slug(v)


class BackfillAgeBody(BaseModel):
    """Body for POST /backfill-age.

    All fields optional with safe defaults — the endpoint accepts an
    empty POST body (e.g. ``curl -X POST .../backfill-age`` with no
    Content-Type) and falls back to ``backfill everything``.

    Wing here is a *filter* (read-side semantic): restrict the backfill
    scope to drawers under one wing. Empty/None means "all wings."
    Normalize via ``rooms.normalize_wing_filter`` so a caller passing
    ``Palace_Daemon`` finds the drawers stored under ``palace_daemon``.
    """

    wing: "str | None" = Field(None, description="Optional wing filter.")
    skip_palace: bool = Field(False, description="Skip Wing/Room/Drawer structure.")
    skip_entities: bool = Field(False, description="Skip per-drawer entity extraction.")
    restart: bool = Field(False, description="Clear checkpoint, start fresh.")

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        return _canon_wing(v)


class SearchAgeFusedBody(BaseModel):
    """Body for POST /search/age-fused."""

    query: str = Field(..., min_length=1)
    wing: "str | None" = Field(None)
    room: "str | None" = Field(None)
    limit: int = Field(10, ge=1, le=200)
    graph_top_k: int = Field(50, ge=1, le=1000)
    fusion_k: int = Field(60, ge=1, le=1000)
    include_trace: bool = Field(False)

    @field_validator("query")
    @classmethod
    def _strip_query(cls, v):
        v = (v or "").strip()
        if not v:
            raise ValueError("'query' is required and must be non-empty")
        return v

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        return _canon_wing(v)

    @field_validator("room")
    @classmethod
    def _validate_room(cls, v):
        return _canon_room(v)
