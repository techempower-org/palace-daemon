"""Closed-vocabulary predicate mapping spike (issue #72).

#50 → PR #61 / #71 established that the AGE knowledge graph carries **64,029**
distinct ``r.relation_type`` predicate strings over 1.72M RELATION triples, and
that the conservative surface-form normalizer (:mod:`kg_predicate_norm`) only
trims ~3% of the *distinct* vocabulary because a long tail of one-off verbose
LLM paraphrases dominates the cardinality.

This module is a **design spike**, not a production write path. It proves
whether a small curated canonical ontology plus embedding-nearest-canonical
mapping can collapse the vocabulary to "dozens" of relation types while
covering the bulk of *triples* (frequency-weighted). The deliverable is
measurement: see ``scripts/canonical_vocab_report.py``.

Pipeline for one raw predicate:

    raw  --kg_predicate_norm.normalize_predicate-->  surface-normalized
         --embed (mempalace MiniLM, same model as the corpus)-->  vector
         --cosine nearest canonical-->  canonical | "other" (if below threshold)

The canonical set (:data:`CANONICAL_RELATIONS`) is a curated ~40-relation
ontology seeded from the highest-frequency post-normalization predicates on
production (``is_a``, ``contains``, ``depends_on``, ``created_by``, …) plus a
handful of schema.org / SKOS-style relations to catch common clusters. Each
canonical has a short gloss; we embed the gloss (not just the bare token) so
the nearest-neighbour match has more semantic surface to bind to.

Embeddings: we reuse ``mempalace.embedding.get_embedding_function`` — the same
ONNX MiniLM (384-dim) the palace embeds drawers with — so no new heavy
dependency is added and predicate similarity is measured in the corpus's own
embedding space. If that import fails, callers can fall back to the pure
lexical scorer (:func:`lexical_similarity`) and the report flags the downgrade.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Optional

from kg_predicate_norm import normalize_predicate

__all__ = [
    "CANONICAL_RELATIONS",
    "Canonical",
    "lexical_similarity",
    "build_embedding_scorer",
    "CanonicalMapper",
]


@dataclass(frozen=True)
class Canonical:
    """A canonical relation: the stored predicate name + a gloss to embed."""

    name: str
    gloss: str


# Curated closed ontology. Seeded from the production frequency table
# (post-normalization top predicates) and rounded out with schema.org / SKOS
# style relations so common LLM paraphrase clusters have a home. The gloss is
# what gets embedded — a short natural phrase binds the nearest-neighbour match
# better than the bare snake_case token.
CANONICAL_RELATIONS: tuple[Canonical, ...] = (
    Canonical("is_a", "is a kind of; is an instance or type of"),
    Canonical("part_of", "is a part or member of; belongs to a whole"),
    Canonical("contains", "contains, includes, or has as a component"),
    Canonical("has_property", "has an attribute, property, or characteristic"),
    Canonical("depends_on", "depends on, requires, or needs to function"),
    Canonical("uses", "uses, utilizes, or makes use of"),
    Canonical("created_by", "was created, authored, written, or made by"),
    Canonical("creates", "creates, generates, or produces something"),
    Canonical("provides", "provides, offers, supplies, or exposes"),
    Canonical("returns", "returns or yields a value or result"),
    Canonical("references", "refers to, points to, or links to"),
    Canonical("located_at", "is located at, in, or positioned somewhere"),
    Canonical("imports", "imports or is imported from a module"),
    Canonical("calls", "calls, invokes, or executes a function"),
    Canonical("implements", "implements, defines, or realizes"),
    Canonical("reads", "reads, loads, or fetches data"),
    Canonical("writes", "writes, sets, stores, or updates a value"),
    Canonical("modifies", "modifies, changes, edits, or alters"),
    Canonical("deletes", "deletes, removes, or drops"),
    Canonical("adds", "adds or appends something"),
    Canonical("handles", "handles, processes, or manages"),
    Canonical("checks", "checks, validates, tests, or verifies"),
    Canonical("matches", "matches, equals, or corresponds to"),
    Canonical("supports", "supports, enables, or allows"),
    Canonical("causes", "causes, triggers, or results in"),
    Canonical("describes", "describes, documents, or explains"),
    Canonical("owns", "owns, manages, or is responsible for"),
    Canonical("assigned_to", "is assigned, allocated, or attributed to"),
    Canonical("derived_from", "is derived, migrated, or copied from"),
    Canonical("related_to", "is related, associated, or connected to"),
    Canonical("works_on", "works on, develops, or contributes to"),
    Canonical("runs", "runs, starts, or executes a process"),
    Canonical("completed", "was completed, finished, or done"),
    Canonical("failed", "failed, errored, crashed, or broke"),
    Canonical("shows", "shows, displays, renders, or presents"),
    Canonical("sends", "sends, transmits, or emits"),
    Canonical("receives", "receives or accepts"),
    Canonical("configures", "configures or sets up"),
    Canonical("mentions", "mentions or names without a stronger relation"),
)


# ─── lexical fallback (no embedding model) ──────────────────────────────
def _tokens(s: str) -> set[str]:
    return {t for t in s.replace("-", "_").split("_") if t}


def lexical_similarity(a: str, b: str) -> float:
    """Jaccard token overlap in [0, 1] — the no-embedding fallback.

    Crude but dependency-free: shared snake_case tokens / union. Used when the
    ONNX embedding model can't be loaded, and the report says so explicitly.
    """
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    return inter / len(ta | tb)


# ─── embedding scorer ────────────────────────────────────────────────────
def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def build_embedding_scorer() -> Optional[Callable[[str, list[str]], list[float]]]:
    """Return ``score(query, candidates) -> [cosine...]`` backed by mempalace's
    ONNX MiniLM, or ``None`` if the model can't be loaded.

    The returned scorer batch-embeds ``[query] + candidates`` in one call and
    returns the cosine of query vs each candidate. Caller decides the
    threshold. Kept as a factory so the (slow) model load happens once and the
    mapper can be constructed lazily / fall back cleanly.
    """
    try:
        from mempalace.embedding import get_embedding_function

        ef = get_embedding_function()
    except Exception:  # pragma: no cover - exercised only without the model
        return None

    def score(query: str, candidates: list[str]) -> list[float]:
        vecs = ef([query, *candidates])
        qv = list(vecs[0])
        return [_cosine(qv, list(v)) for v in vecs[1:]]

    return score


class CanonicalMapper:
    """Maps raw predicates to a closed canonical relation set.

    Construction embeds each canonical's gloss once (or, in lexical mode, just
    holds the canonical names). :meth:`map_predicate` then surface-normalizes
    the raw predicate, scores it against every canonical, and returns the best
    match if it clears ``threshold`` — otherwise ``"other"`` (the explicit
    long-tail bucket). A dropped predicate (code token, per
    ``normalize_predicate``) maps to ``None``.
    """

    def __init__(
        self,
        threshold: float = 0.45,
        scorer: Optional[Callable[[str, list[str]], list[float]]] = None,
        use_embeddings: bool = True,
    ):
        self.threshold = threshold
        self.canonicals = CANONICAL_RELATIONS
        self._names = [c.name for c in self.canonicals]
        self._glosses = [c.gloss for c in self.canonicals]
        self.mode: str

        if use_embeddings:
            self._scorer = scorer if scorer is not None else build_embedding_scorer()
            self.mode = "embedding" if self._scorer is not None else "lexical"
        else:
            self._scorer = None
            self.mode = "lexical"

        # Pre-embed canonical glosses for the embedding path so each
        # map_predicate call only embeds the (one) query string.
        self._gloss_vecs: Optional[list[list[float]]] = None
        if self.mode == "embedding":
            try:
                from mempalace.embedding import get_embedding_function

                ef = get_embedding_function()
                self._ef = ef
                self._gloss_vecs = [list(v) for v in ef(self._glosses)]
            except Exception:  # pragma: no cover
                self.mode = "lexical"
                self._scorer = None

    def _scores(self, normalized: str) -> list[float]:
        if self.mode == "embedding" and self._gloss_vecs is not None:
            qv = list(self._ef([normalized])[0])
            return [_cosine(qv, gv) for gv in self._gloss_vecs]
        # lexical fallback: score against canonical *names* (token overlap)
        return [lexical_similarity(normalized, name) for name in self._names]

    def map_predicate(self, raw: str) -> tuple[Optional[str], float]:
        """Return (canonical_or_other_or_None, score).

        * ``None`` — dropped by ``normalize_predicate`` (code token / junk).
        * ``"other"`` — kept but below threshold (the long-tail bucket).
        * canonical name — nearest canonical at/above threshold.
        """
        normalized = normalize_predicate(raw)
        if normalized is None:
            return None, 0.0
        # exact canonical hit short-circuits the scorer
        if normalized in self._names:
            return normalized, 1.0
        scores = self._scores(normalized)
        best_i = max(range(len(scores)), key=lambda i: scores[i])
        best = scores[best_i]
        if best >= self.threshold:
            return self._names[best_i], best
        return "other", best
