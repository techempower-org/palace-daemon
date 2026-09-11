"""Cypher source rewrites applied before handing caller SQL to Apache AGE.

Currently one rewrite: resolving projection aliases referenced from
``ORDER BY`` (palace-daemon#209).

AGE resolves ``ORDER BY`` identifiers against the range table of the
clauses *preceding* the projection, not against the projection's own
alias list. An alias introduced by ``... AS k`` therefore has no range
table entry when the sort clause is transformed, and AGE errors with
``could not find rte for k``. Ordering by the *expression* that defined
the alias works, because that expression's inputs do live in the
preceding range table.

Measured against production AGE (mempalace_kg, 2026-09-10) with
zero-scan ``UNWIND`` probes::

    RETURN x AS k ORDER BY k                              -> could not find rte for k
    RETURN x AS k, count(*) AS n ORDER BY n DESC          -> could not find rte for n
    RETURN x AS k, count(*) AS n ORDER BY (count(*)) DESC -> 200 OK
    WITH x AS k ORDER BY k                                -> could not find rte for k
    WITH x AS k ORDER BY (x)                              -> 200 OK

Note the defect is not aggregate-specific: a plain non-aggregate alias
fails identically. ``WITH`` projections are affected exactly like
``RETURN`` ones, so both are rewritten.

The rewrite is deliberately conservative. Anything it cannot parse with
confidence is returned untouched — a caller-visible 400 from AGE is a far
better outcome than silently mangling someone's query.
"""
from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

__all__ = ["rewrite_order_by_aliases"]

_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<comment>//[^\n]*)
    | (?P<str>'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")
    | (?P<param>\$[A-Za-z_][A-Za-z_0-9]*)
    | (?P<ident>[A-Za-z_][A-Za-z_0-9]*)
    | (?P<num>\d+(?:\.\d+)?)
    | (?P<punct>.)
    """,
    re.VERBOSE | re.DOTALL,
)

_OPENERS = {"(": ")", "[": "]", "{": "}"}
_CLOSERS = {")", "]", "}"}

# Keywords that end a projection body or an ORDER BY body at depth 0.
_CLAUSE_STARTERS = frozenset(
    {
        "MATCH", "OPTIONAL", "WHERE", "UNWIND", "CREATE", "MERGE", "SET",
        "DELETE", "DETACH", "REMOVE", "CALL", "YIELD", "UNION", "FOREACH",
        "WITH", "RETURN", "ORDER", "SKIP", "LIMIT", "USING", "LOAD",
    }
)

_PROJECTION_KEYWORDS = frozenset({"RETURN", "WITH"})

# Words that may appear bare inside an ORDER BY body but are never a
# reference to a projection alias.
_ORDER_BY_KEYWORDS = frozenset(
    {
        "ASC", "ASCENDING", "DESC", "DESCENDING", "AND", "OR", "XOR", "NOT",
        "IS", "NULL", "TRUE", "FALSE", "IN", "STARTS", "ENDS", "CONTAINS",
        "CASE", "WHEN", "THEN", "ELSE", "END", "DISTINCT",
    }
)


class _Unparseable(Exception):
    """Raised when the source cannot be tokenised into balanced clauses."""


class _Tok:
    __slots__ = ("depth", "end", "kind", "start", "text")

    def __init__(self, kind, text, start, end, depth):
        self.kind = kind
        self.text = text
        self.start = start
        self.end = end
        self.depth = depth

    @property
    def upper(self) -> str:
        return self.text.upper()


def _tokenize(src: str) -> list:
    """Tokenise Cypher, dropping whitespace/comments, tracking bracket depth.

    A bracket token carries the depth *outside* itself, so an opener and
    its matching closer share a depth.
    """
    toks = []
    depth = 0
    for m in _TOKEN_RE.finditer(src):
        kind = m.lastgroup
        if kind in ("ws", "comment"):
            continue
        text = m.group()
        if kind == "punct" and text in _CLOSERS:
            depth -= 1
            if depth < 0:
                raise _Unparseable(f"unbalanced {text!r}")
        toks.append(_Tok(kind, text, m.start(), m.end(), depth))
        if kind == "punct" and text in _OPENERS:
            depth += 1
    if depth != 0:
        raise _Unparseable("unbalanced brackets")
    return toks


def _is_clause_boundary(tok: _Tok) -> bool:
    return tok.depth == 0 and tok.kind == "ident" and tok.upper in _CLAUSE_STARTERS


def _iter_projection_order_by(toks: list):
    """Yield ``(projection_tokens, order_by_tokens)`` for each RETURN/WITH
    that is immediately followed by its own ORDER BY clause."""
    n = len(toks)
    for i, tok in enumerate(toks):
        if tok.depth != 0 or tok.kind != "ident" or tok.upper not in _PROJECTION_KEYWORDS:
            continue
        j = i + 1
        while j < n and not _is_clause_boundary(toks[j]):
            j += 1
        if j + 1 >= n or toks[j].upper != "ORDER" or toks[j + 1].upper != "BY":
            continue
        k = j + 2
        while k < n and not _is_clause_boundary(toks[k]):
            k += 1
        projection = toks[i + 1 : j]
        order_by = toks[j + 2 : k]
        if projection and order_by:
            yield projection, order_by


def _split_top_level(toks: list) -> list:
    """Split a projection body on depth-0 commas."""
    items, current = [], []
    for tok in toks:
        if tok.kind == "punct" and tok.text == "," and tok.depth == 0:
            items.append(current)
            current = []
        else:
            current.append(tok)
    items.append(current)
    return items


def _alias_map(projection: list, src: str) -> dict:
    """Map ``alias -> defining expression text`` for one projection body."""
    aliases = {}
    for index, item in enumerate(_split_top_level(projection)):
        if not item:
            continue
        if index == 0 and item[0].kind == "ident" and item[0].upper == "DISTINCT":
            item = item[1:]
            if not item:
                continue
        as_pos = None
        for pos, tok in enumerate(item):
            if tok.kind == "ident" and tok.depth == 0 and tok.upper == "AS":
                as_pos = pos
        if as_pos is None or as_pos == 0 or as_pos + 1 >= len(item):
            continue
        alias_tok = item[as_pos + 1]
        if alias_tok.kind != "ident":
            continue
        expression = src[item[0].start : item[as_pos - 1].end].strip()
        if expression:
            aliases[alias_tok.text] = expression
    return aliases


def _substitutions(order_by: list, aliases: dict) -> list:
    """Locate alias references inside an ORDER BY body.

    Skips property lookups (``x.n``), function names (``n(...)``), map
    keys (``{n: ...}``) and sort/boolean keywords.
    """
    subs = []
    for pos, tok in enumerate(order_by):
        if tok.kind != "ident" or tok.upper in _ORDER_BY_KEYWORDS:
            continue
        expression = aliases.get(tok.text)
        if expression is None:
            continue
        prev = order_by[pos - 1] if pos > 0 else None
        nxt = order_by[pos + 1] if pos + 1 < len(order_by) else None
        if prev is not None and prev.kind == "punct" and prev.text == ".":
            continue
        if nxt is not None and nxt.kind == "punct" and nxt.text in ("(", ":"):
            continue
        subs.append((tok.start, tok.end, f"({expression})"))
    return subs


def rewrite_order_by_aliases(cypher):
    """Replace projection-alias references in ORDER BY with their expressions.

    ``RETURN count(*) AS n ORDER BY n DESC`` becomes
    ``RETURN count(*) AS n ORDER BY (count(*)) DESC``, which AGE accepts.

    Returns the input unchanged (never raises) when there is nothing to do
    or when the source cannot be parsed confidently.
    """
    if not isinstance(cypher, str) or not cypher.strip():
        return cypher
    try:
        toks = _tokenize(cypher)
        subs = []
        for projection, order_by in _iter_projection_order_by(toks):
            aliases = _alias_map(projection, cypher)
            if aliases:
                subs.extend(_substitutions(order_by, aliases))
    except Exception as e:
        # Rewriting is best-effort: a query we cannot parse is passed through
        # untouched rather than mangled. Logged so operators can see why a
        # caller still got "could not find rte for ...".
        _log.warning("ORDER BY alias rewrite skipped for %r: %s", cypher[:120], e)
        return cypher
    if not subs:
        return cypher
    out = cypher
    for start, end, replacement in sorted(subs, key=lambda s: s[0], reverse=True):
        out = out[:start] + replacement + out[end:]
    return out
