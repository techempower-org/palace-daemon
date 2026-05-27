"""Guarded post-extraction canonical-mapping write pass (issue #72, approach a).

The #74 spike (:mod:`kg_canonical_vocab`) showed a 39-relation closed ontology +
embedding-nearest-canonical mapping collapses the 64k live predicate vocabulary
to dozens while covering ~65% of triples. Approach (a) applies that mapping
**at write time**, before a RELATION edge is persisted, so new triples land on a
canonical ``relation_type`` instead of a freeform LLM string.

## Where the write actually happens

IMPORTANT — the RELATION write path is **not** in palace-daemon. The daemon only
*reads* the KG; triples are extracted and persisted by the mempalace package's
``kg_triple_worker`` (a separate process, ``python -m mempalace ...``), via
``add_triple(... predicate ...)``. The daemon cannot intercept that write from
its own process.

So this module is the **seam**, not the wiring:

* :func:`map_for_write` is a pure, dependency-light decision function that the
  worker (or any writer) calls to turn a raw predicate into the
  ``(relation_type, raw_relation_type)`` pair to persist. palace-daemon owns and
  tests this logic.
* Actually invoking it from ``kg_triple_worker`` is the upstream/(b) piece — a
  one-line, default-OFF call documented in the PR. It is gated so merging this
  changes nothing in production until the flag is flipped.

## Guard

``PALACE_KG_CANONICAL_MAPPING`` env var, read live per call:

* unset / ``"0"`` / ``"false"`` / ``"off"`` (default) → **pass-through**: the
  raw predicate is the relation_type and there is no raw_relation_type. Behavior
  is byte-for-byte the current behavior.
* ``"1"`` / ``"true"`` / ``"on"`` → map the predicate to its canonical (or the
  ``other`` bucket) and **retain the original** as ``raw_relation_type`` so the
  mapping is fully reversible.

The embedding mapper is loaded lazily and cached, so a process that never flips
the flag never pays the model-load cost.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Optional

__all__ = ["mapping_enabled", "map_for_write", "MappedPredicate", "reset_mapper_cache"]

_TRUE = {"1", "true", "on", "yes"}
_FLAG = "PALACE_KG_CANONICAL_MAPPING"

# Below this cosine the predicate is parked in the explicit long-tail bucket
# rather than forced onto a poorly-fitting canonical. Matches the #74 spike's
# reported operating point.
_DEFAULT_THRESHOLD = 0.45
_OTHER_BUCKET = "other"

_mapper = None
_mapper_lock = threading.Lock()


@dataclass(frozen=True)
class MappedPredicate:
    """Result of the write pass for one predicate.

    * ``relation_type`` — what to store on the edge (canonical, ``other``, or
      the raw string when mapping is disabled / the predicate is a code token
      that should still be written verbatim).
    * ``raw_relation_type`` — the original predicate, retained for reversibility
      when mapping changed it; ``None`` when unchanged or mapping disabled.
    * ``dropped`` — True if the predicate is a code token the spike would drop
      entirely (the caller decides whether to skip the triple).
    * ``mapped`` — True if a canonical mapping was actually applied.
    """

    relation_type: Optional[str]
    raw_relation_type: Optional[str]
    dropped: bool
    mapped: bool


def mapping_enabled() -> bool:
    """Read the guard flag live (so it can be toggled without a restart)."""
    return os.environ.get(_FLAG, "").strip().lower() in _TRUE


def reset_mapper_cache() -> None:
    """Drop the cached mapper (tests use this to swap flag/threshold)."""
    global _mapper
    with _mapper_lock:
        _mapper = None


def _get_mapper():
    global _mapper
    if _mapper is None:
        with _mapper_lock:
            if _mapper is None:
                from kg_canonical_vocab import CanonicalMapper

                threshold = float(
                    os.environ.get("PALACE_KG_CANONICAL_THRESHOLD", _DEFAULT_THRESHOLD)
                )
                _mapper = CanonicalMapper(threshold=threshold)
    return _mapper


def map_for_write(raw_predicate: str) -> MappedPredicate:
    """Decide what relation_type to persist for ``raw_predicate``.

    Disabled (default): pass-through — ``relation_type == raw_predicate``, no
    raw retained, never dropped. Identical to current behavior.

    Enabled: map to canonical via :class:`kg_canonical_vocab.CanonicalMapper`.
    A code token maps to ``dropped=True`` (caller skips the triple). Otherwise
    the canonical (or ``other``) becomes ``relation_type`` and the original is
    retained as ``raw_relation_type`` whenever it differs.
    """
    if not mapping_enabled():
        return MappedPredicate(
            relation_type=raw_predicate,
            raw_relation_type=None,
            dropped=False,
            mapped=False,
        )

    canon, _score = _get_mapper().map_predicate(raw_predicate)
    if canon is None:
        # code token / junk — the spike drops these
        return MappedPredicate(
            relation_type=None,
            raw_relation_type=raw_predicate,
            dropped=True,
            mapped=True,
        )
    changed = canon != raw_predicate
    return MappedPredicate(
        relation_type=canon,
        raw_relation_type=raw_predicate if changed else None,
        dropped=False,
        mapped=changed,
    )
