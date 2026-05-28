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
from typing import Callable, Iterable, Optional

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

    def map_predicates(
        self,
        raws: Iterable[str],
        batch_size: int = 1024,
    ) -> list[tuple[Optional[str], float]]:
        """Batched equivalent of :meth:`map_predicate` for bulk callers.

        Same per-element semantics as the per-call API — failed-normalize,
        canonical short-circuit, embedding nearest-neighbour, threshold gate,
        ``"other"`` long-tail bucket — but collapses the embedding cost from N
        single-string ``ef()`` calls into ceil(N/batch_size) batch calls. Output
        is aligned 1:1 with the input iterable (one tuple per input, in order),
        so callers can ``zip(raws, mapper.map_predicates(raws))`` without
        bookkeeping.

        Returns
        -------
        list[tuple[Optional[str], float]]
            Per-input result, same shape as :meth:`map_predicate`:

            * ``(None, 0.0)`` — dropped by ``normalize_predicate``.
            * ``(name, 1.0)`` — normalized form is exactly a canonical name.
            * ``(canonical, score)`` — nearest canonical at/above threshold.
            * ``("other", score)`` — kept but below threshold.

        ## Performance

        Measured 2026-05-27 on the live ~64k-predicate vocabulary, embedding
        mode, batch_size=1024:

        * GPU (per-call ``map_predicate``): 64,801 raws in ~7 min (~155/s)
        * GPU (batched ``map_predicates``): 64,801 raws in ~74 s (~880/s)
        * CPU (per-call): ~21 min for the same input

        The batched path's win comes from amortising Python-side model launch
        overhead — each ``ef()`` call would otherwise dispatch a batch of 1 +
        the 39 pre-cached canonicals. Lexical mode does NOT benefit from
        batching (the scoring is pure-Python token overlap); the implementation
        falls back to a per-call loop there. Reach for this method when N is
        in the thousands and the mapper is in embedding mode; below ~100 raws
        the overhead crossover makes the per-call API equivalent or faster.

        The next caller in the tree is
        ``palace-daemon/scripts/canonical_migration.py``, which re-runs the
        mapping over the live AGE predicate vocabulary; replacing its per-call
        loop with this method cuts the embedding-mode migration from minutes
        to under two.
        """
        materialized = list(raws)
        results: list[tuple[Optional[str], float]] = [(None, 0.0)] * len(materialized)
        if not materialized:
            return results

        # Lexical mode (or any non-embedding mode): batching offers no win.
        # Mirror the per-call path exactly so callers get identical output.
        if self.mode != "embedding" or self._gloss_vecs is None:
            return [self.map_predicate(r) for r in materialized]

        # Embedding mode: pre-normalize and short-circuit before touching ef().
        # `pending` collects (original_index, normalized) for raws that still
        # need an embedding-based score; everything else is filled in place.
        pending: list[tuple[int, str]] = []
        names_set = set(self._names)  # O(1) short-circuit membership
        for i, raw in enumerate(materialized):
            normalized = normalize_predicate(raw)
            if normalized is None:
                results[i] = (None, 0.0)
                continue
            if normalized in names_set:
                results[i] = (normalized, 1.0)
                continue
            pending.append((i, normalized))

        if not pending:
            return results

        # Pre-normalize the canonical gloss matrix once. Stack as a 2D numpy
        # array so the per-batch query embeddings can be dotted against it in
        # a single matmul.
        try:
            import numpy as np
        except Exception:  # pragma: no cover - numpy is a hard transitive dep
            # No numpy → fall back to per-call. Should not happen in any
            # supported palace-daemon environment but keeps the public API
            # honest if numpy ever becomes optional.
            for i, normalized in pending:
                results[i] = self.map_predicate(materialized[i])
            return results

        gloss_matrix = np.asarray(self._gloss_vecs, dtype=np.float64)
        gloss_norms = np.linalg.norm(gloss_matrix, axis=1, keepdims=True)
        # Avoid divide-by-zero on any all-zero gloss vector (degenerate).
        gloss_norms = np.where(gloss_norms == 0.0, 1.0, gloss_norms)
        gloss_unit = gloss_matrix / gloss_norms

        threshold = self.threshold
        names = self._names

        for start in range(0, len(pending), batch_size):
            chunk = pending[start : start + batch_size]
            query_strs = [normalized for _i, normalized in chunk]
            query_vecs = np.asarray(self._ef(query_strs), dtype=np.float64)
            q_norms = np.linalg.norm(query_vecs, axis=1, keepdims=True)
            q_norms = np.where(q_norms == 0.0, 1.0, q_norms)
            query_unit = query_vecs / q_norms
            scores = query_unit @ gloss_unit.T  # (len(chunk), len(canonicals))
            best_i = np.argmax(scores, axis=1)
            best_scores = scores[np.arange(len(chunk)), best_i]
            for row, (orig_i, _normalized) in enumerate(chunk):
                score = float(best_scores[row])
                if score >= threshold:
                    results[orig_i] = (names[int(best_i[row])], score)
                else:
                    results[orig_i] = ("other", score)
        return results
