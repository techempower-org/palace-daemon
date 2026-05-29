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
    # rerank (#189): per-request cross-encoder override; None → env default.
    rerank: "bool | None" = Field(None, description="Per-request rerank toggle; None defers to PALACE_RERANK_ENABLED.")

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
    # rerank (#189): per-request cross-encoder override. None → fall back
    # to PALACE_RERANK_ENABLED; True/False forces the stage on/off for this
    # request only, so ablation benches can A/B rerank within one pass.
    rerank: "bool | None" = Field(None)

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


# /mine accepts these enum values (mirrors _MINE_VALID_MODES /
# _MINE_VALID_EXTRACTS in main.py). Kept module-private here because
# the pydantic validator needs them at parse time; main.py still owns
# the canonical sets so `from main import _MINE_VALID_MODES` style
# isn't necessary — these strings rarely change.
_MINE_MODES = ("convos", "projects", "session")
_MINE_EXTRACTS = ("exchange", "general")


class MineBody(BaseModel):
    """Body for POST /mine (kick off a mempalace mine subprocess).

    Write-side wing semantics: default "general" (matching the pre-#178
    inline behavior); empty input coerces to "general" then normalizes
    via _normalize_wing_slug. Filesystem checks on ``dir`` (exists,
    is_dir, no traversal) stay in the handler — pydantic can't probe
    the filesystem at parse time, and we want consistent 400 messages
    naming the offending path.
    """

    dir: str = Field(..., min_length=1, description="Absolute path to mine (required).")
    wing: str = Field("general", description="Wing slug — normalized; empty → 'general'.")
    mode: str = Field("convos", description="Mine mode.")
    extract: "str | None" = Field(None, description="Optional extract policy.")
    limit: "int | None" = Field(None, ge=1, description="Optional drawer-count cap.")

    @field_validator("dir")
    @classmethod
    def _require_dir(cls, v):
        if not isinstance(v, str):
            # pydantic enforces string via type hint, but this guards
            # against JSON null / number / object that would have crashed
            # _translate_client_path earlier.
            raise ValueError("'dir' must be a string")
        if not v.strip():
            raise ValueError("'dir' is required and must be non-empty")
        return v

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        from rooms import normalize_wing_slug
        # Empty input defaults to "general" (pre-#178 contract), then
        # normalized as a write-side wing.
        if not v or not v.strip():
            v = "general"
        return normalize_wing_slug(v)

    @field_validator("mode")
    @classmethod
    def _validate_mode(cls, v):
        if v not in _MINE_MODES:
            raise ValueError(f"'mode' must be one of: {', '.join(sorted(_MINE_MODES))}")
        return v

    @field_validator("extract")
    @classmethod
    def _validate_extract(cls, v):
        if v is None:
            return v
        if v not in _MINE_EXTRACTS:
            raise ValueError(f"'extract' must be one of: {', '.join(sorted(_MINE_EXTRACTS))}")
        return v


class MemoryBody(BaseModel):
    """Body for POST /memory (the primary write surface).

    Write-side wing semantics: empty input coerces to ``"unknown"`` (the
    pre-#179 inline default), then normalizes through ``normalize_wing_slug``.
    This is distinct from /silent-save (which preserves ``""`` and lets the
    handler emit a warning) and /mine (which defaults to ``"general"``).

    Room defaults to ``"discoveries"`` (the spec's catch-all per the canonical
    7-room taxonomy) when missing or empty, then is validated via
    ``rooms.validate_room_or_raise`` which produces a structured 400 with
    ``valid_rooms`` + ``hint`` on a typo. The structured detail matches the
    pre-#179 inline error shape exactly — clients that parsed
    ``detail.valid_rooms`` continue to work.

    Content is permissive — empty strings round-trip through to mempalace.
    The pre-#179 handler did ``body.get("content", "")`` with no rejection,
    so this preserves behavior even though min_length=1 might feel safer.
    A future PR could tighten it after auditing for callers that intentionally
    write empty drawers (none known in-tree).

    NOTE on ``validate_default=True`` (the model_config below): pydantic v2
    skips field validators on default values unless this is enabled. The
    pre-#179 handler used ``body.get("wing") or "unknown"`` / ``body.get(
    "room") or "discoveries"`` to coerce both missing AND empty inputs to
    the canonical default. Without validate_default, a request with no
    ``wing`` / ``room`` keys would arrive in the handler as ``""`` instead
    of being coerced to the canonical defaults — and mempalace would reject
    it as "room is empty after sanitization". The first deploy of MemoryBody
    actually shipped with this regression; this docstring is the receipt.
    """

    model_config = {"validate_default": True}

    content: str = Field("", description="Drawer content body (empty allowed for back-compat).")
    wing: str = Field("", description="Wing slug — empty → 'unknown', then normalized.")
    room: str = Field("", description="Canonical room — empty → 'discoveries', validated.")

    @field_validator("wing")
    @classmethod
    def _normalize_wing(cls, v):
        # /memory has WRITE-side wing semantics: empty coerces to the
        # 'unknown' default (matching pre-#179 inline behavior) and then
        # normalizes via the write helper. Distinct from /silent-save
        # which preserves "" and from /backfill-age which treats empty
        # as None (filter mode).
        from rooms import normalize_wing_slug
        if not v or not v.strip():
            v = "unknown"
        return normalize_wing_slug(v)

    @field_validator("room")
    @classmethod
    def _validate_room(cls, v):
        # /memory defaults empty room to "discoveries" (spec's catch-all)
        # then validates against the canonical set. validate_room_or_raise
        # produces the structured 400 with valid_rooms + hint.
        if not v or not v.strip():
            v = "discoveries"
        # _canon_room is the shared helper at the top of this module;
        # it raises HTTPException(400, detail={...}) on a non-canonical
        # value. The HTTPException propagates cleanly through pydantic.
        return _canon_room(v)


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
    # rerank (#189): per-request cross-encoder override; None → env default.
    rerank: "bool | None" = Field(None)

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
