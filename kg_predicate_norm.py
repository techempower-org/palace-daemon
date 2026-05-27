"""Predicate normalization for the AGE knowledge graph (issue #50).

The LLM triple extractor emits ~1000+ distinct ``relation_type`` strings.
Three classes of contamination bloat the predicate vocabulary far past the
underlying semantic relation count:

1. **Code tokens** treated as predicates (``appendchild``, ``createelement``,
   ``executemany``, ``setattribute``, ``getelementbyid``) — JS/Python API
   method names the extractor pulled out of source-code drawers. These should
   be dropped.
2. **Near-synonyms** not collapsed (``is`` / ``is_a`` / ``is_an_instance_of``;
   ``was_a`` / ``is_a_kind_of``) — canonicalized to one relation type.
3. **Grammatical fragments** with negation/punctuation glued in
   (``don't_adapt``, ``aren't_merged``, ``'doesn't_appear'``) — apostrophes
   and quotes are stripped and the negation polarity is denormalized into a
   ``not_<base>`` form rather than left as an arbitrary contraction.

This is a **pure module** — no DB, no AGE imports, no network. The single
public entry point is :func:`normalize_predicate`, which returns the
canonical predicate string or ``None`` to signal "drop this triple". That
makes it trivially unit-testable and safe to run as a read-only dry-run pass
over the live vocabulary without touching the graph.

Wiring this into the write path is the daemon's choice and must be opt-in;
this module never mutates anything on its own.
"""
from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "normalize_predicate",
    "CODE_TOKEN_BLOCKLIST",
    "SYNONYM_MAP",
    "NEGATION_PREFIXES",
]


# ─── Class 1: code-token blocklist ──────────────────────────────────────
# Method / DOM / DB-API names the extractor mistook for relations. These are
# camelCase or lowercase identifiers with no semantic relation meaning.
#
# The extractor emits these in mixed case (``appendChild`` *and*
# ``appendchild``). Folding splits camelCase into snake_case, so we match
# against the blocklist in BOTH the camel-split form (``append_child``) and
# the de-underscored form (``appendchild``) — see ``_is_code_token``. Entries
# below are therefore listed once in the bare-lowercase form (issue-observed)
# and the matcher handles the camel-split variant.
#
# Kept as an explicit blocklist rather than a heuristic because a pure
# "drop all single lowercase tokens" rule would also kill legitimate verbs
# like ``uses`` / ``owns``. The blocklist is conservative and additive.
CODE_TOKEN_BLOCKLIST: frozenset[str] = frozenset(
    {
        # DOM API (issue examples)
        "appendchild",
        "createelement",
        "setattribute",
        "getelementbyid",
        "getelementsbyclassname",
        "getelementsbytagname",
        "queryselector",
        "queryselectorall",
        "addeventlistener",
        "removeeventlistener",
        "removechild",
        "insertbefore",
        "createtextnode",
        "getattribute",
        "classlist",
        "innerhtml",
        "textcontent",
        # DB-API / ORM (issue examples)
        "executemany",
        "executescript",
        "fetchone",
        "fetchall",
        "fetchmany",
        "rowcount",
        "lastrowid",
        # generic stdlib / language method noise
        "tostring",
        "valueof",
        "hasownproperty",
        "getattr",
        "setattr",
        "hasattr",
        "delattr",
    }
)


# ─── Class 2: synonym → canonical map ────────────────────────────────────
# Conservative collapse. We merge only relations that are clearly the *same*
# semantic edge under surface-form / tense / article variation. We do NOT
# merge semantically distinct relations (``part_of`` stays separate from
# ``is_a``; ``created_by`` stays separate from ``owned_by``).
#
# Keys are post-fold (snake_case, lowercased, punctuation-stripped) forms.
# Values are the chosen canonical form.
SYNONYM_MAP: dict[str, str] = {
    # identity / instance-of family → is_a
    "is": "is_a",
    "is_an": "is_a",
    "are": "is_a",
    "was": "is_a",
    "was_a": "is_a",
    "was_an": "is_a",
    "were": "is_a",
    "is_a_kind_of": "is_a",
    "is_a_type_of": "is_a",
    "is_an_instance_of": "is_a",
    "is_instance_of": "is_a",
    "instance_of": "is_a",
    "a_kind_of": "is_a",
    "type_of": "is_a",
    "kind_of": "is_a",
    # composition family → part_of
    "is_part_of": "part_of",
    "is_a_part_of": "part_of",
    "a_part_of": "part_of",
    "belongs_to": "part_of",
    "member_of": "part_of",
    # reference family → references
    "is_a_reference": "references",
    "is_a_reference_to": "references",
    "is_reference_to": "references",
    "reference": "references",
    "refers_to": "references",
    "references_to": "references",
    # usage family → uses
    "use": "uses",
    "used": "uses",
    "uses_a": "uses",
    "makes_use_of": "uses",
    "utilizes": "uses",
    # dependency family → depends_on
    "depend_on": "depends_on",
    "depends_upon": "depends_on",
    "requires": "depends_on",
    "relies_on": "depends_on",
    # authorship family → created_by
    "authored_by": "created_by",
    "written_by": "created_by",
    "made_by": "created_by",
    "built_by": "created_by",
    # containment family → contains
    "contain": "contains",
    "includes": "contains",
    "has": "contains",
    "have": "contains",
    # work family → works_on
    "work_on": "works_on",
    "working_on": "works_on",
    "works_with": "works_on",
}


# ─── Class 3: negation handling ──────────────────────────────────────────
# Contractions and negation words the extractor glues into the predicate.
# We strip the negation off, normalize the remaining base, and re-prefix
# with a uniform ``not_`` so polarity lives in a predictable facet rather
# than fanning out across ``dont_*`` / ``arent_*`` / ``doesnt_*`` variants.
#
# Order matters: longer/more-specific prefixes first so ``does_not_`` is not
# shadowed by ``not_``.
NEGATION_PREFIXES: tuple[str, ...] = (
    "does_not_",
    "do_not_",
    "did_not_",
    "is_not_",
    "are_not_",
    "was_not_",
    "were_not_",
    "has_not_",
    "have_not_",
    "had_not_",
    "can_not_",
    "could_not_",
    "should_not_",
    "would_not_",
    "will_not_",
    "doesnt_",
    "dont_",
    "didnt_",
    "isnt_",
    "arent_",
    "wasnt_",
    "werent_",
    "hasnt_",
    "havent_",
    "hadnt_",
    "cant_",
    "cannot_",
    "couldnt_",
    "shouldnt_",
    "wouldnt_",
    "wont_",
    "not_",
)


# Identifiers that are pure code noise even if not in the blocklist: a single
# lowercase run with an embedded digit or that is implausibly long for a verb
# phrase. Kept narrow on purpose — see _looks_like_code.
_DIGIT_RE = re.compile(r"\d")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_PUNCT_STRIP_RE = re.compile(r"[\"'`’‘“”]")
_NONWORD_RE = re.compile(r"[^a-z0-9_]+")
_MULTI_US_RE = re.compile(r"_{2,}")


def _fold(raw: str) -> str:
    """Lowercase + snake_case + strip punctuation, mirroring the extractor's
    ``_normalize_predicate`` but with apostrophe/quote stripping (class 3).

    Steps:
      * split camelCase boundaries so ``appendChild`` → ``append_child``
        and matches the blocklist's folded forms
      * strip surrounding/embedded quotes and apostrophes
      * lowercase, spaces/hyphens → underscore
      * drop any remaining non-word chars, collapse repeated underscores
    """
    s = _CAMEL_BOUNDARY_RE.sub("_", raw.strip())
    s = _PUNCT_STRIP_RE.sub("", s)
    s = s.lower().replace(" ", "_").replace("-", "_")
    s = _NONWORD_RE.sub("_", s)
    s = _MULTI_US_RE.sub("_", s)
    return s.strip("_")


def _looks_like_code(folded: str) -> bool:
    """Heuristic for code tokens beyond the explicit blocklist.

    Conservative: only flags single-token identifiers (no underscores) that
    also contain a digit (``utf8decode``, ``sha256hash``). A bare verb like
    ``uses`` has no digit and survives; a snake_case phrase like
    ``works_on`` has an underscore and survives.
    """
    if "_" in folded:
        return False
    return bool(_DIGIT_RE.search(folded))


def _is_code_token(folded: str) -> bool:
    """True if ``folded`` is a code token to drop.

    Checks the blocklist against both the camel-split form (``append_child``)
    and the de-underscored form (``appendchild``), since the extractor emits
    method names in mixed case and the fold normalizes camelCase to
    snake_case. Also applies the digit heuristic.
    """
    if folded in CODE_TOKEN_BLOCKLIST:
        return True
    if folded.replace("_", "") in CODE_TOKEN_BLOCKLIST:
        return True
    return _looks_like_code(folded)


# Negation words that, standing alone (after the trailing underscore is
# folded away), carry no relation — e.g. a raw predicate of just ``not_``
# folds to ``not`` and must drop.
_BARE_NEGATION_WORDS: frozenset[str] = frozenset(
    p.rstrip("_") for p in NEGATION_PREFIXES
)


def _strip_negation(folded: str) -> tuple[str, bool]:
    """Return (base, negated). Strips a leading negation prefix if present."""
    for prefix in NEGATION_PREFIXES:
        if folded.startswith(prefix) and len(folded) > len(prefix):
            return folded[len(prefix):], True
    return folded, False


def _canonicalize(base: str) -> str:
    """Apply the synonym map (idempotent — canonical forms map to themselves
    or are absent, which is a no-op)."""
    return SYNONYM_MAP.get(base, base)


def normalize_predicate(raw: str) -> Optional[str]:
    """Normalize a raw extractor predicate to its canonical form.

    Returns the canonical predicate string, or ``None`` to signal that the
    triple should be dropped (code token, empty, or punctuation-only input).

    Pipeline:
      1. fold — lowercase, snake_case, strip quotes/apostrophes (class 3 prep)
      2. drop if empty or a known/heuristic code token (class 1)
      3. strip negation prefix, remember polarity (class 3)
      4. canonicalize the base via the synonym map (class 2)
      5. re-apply ``not_`` prefix if it was negated

    The negation prefix is applied *after* canonicalization so that
    ``doesn't_appear`` and ``does_not_appear`` both land on ``not_appear``,
    and a negated synonym (``is not a part of`` → base ``a_part_of`` →
    ``part_of``) collapses to ``not_part_of``.

    Note the base is whatever remains *after* the negation prefix is peeled:
    ``isn't_a`` strips ``isnt_`` leaving base ``a`` (not in ``SYNONYM_MAP``),
    so it yields ``not_a``, not ``not_is_a``.
    """
    if not isinstance(raw, str):
        return None

    folded = _fold(raw)
    if not folded:
        return None

    # A bare negation word with nothing attached (raw ``not_`` → folds to
    # ``not``) carries no relation.
    if folded in _BARE_NEGATION_WORDS:
        return None

    # Class 1: code tokens are dropped outright (no negation/synonym pass).
    if _is_code_token(folded):
        return None

    # Class 3: peel negation, normalize the base, then re-prefix.
    base, negated = _strip_negation(folded)
    if not base:
        # The whole token was a negation prefix (e.g. "not_"); nothing left.
        return None

    # A dropped base (code token hiding behind a negation) drops the whole
    # predicate too.
    if _is_code_token(base):
        return None

    # Class 2: collapse synonyms on the base.
    canonical = _canonicalize(base)

    return f"not_{canonical}" if negated else canonical
