#!/usr/bin/env python3
"""Closed-vocabulary mapping dry-run report (issue #72).

Pulls the live predicate vocabulary **with frequencies** (READ-ONLY `/cypher`
GROUP BY over `r.relation_type`), maps every distinct predicate to the closed
canonical set in :mod:`kg_canonical_vocab`, and reports the signal that tells
JP whether a closed vocabulary is viable:

  * raw distinct cardinality vs mapped canonical cardinality
  * **% of TRIPLES covered** by a canonical relation (frequency-weighted) —
    the headline number, since the long tail dominates *distinct* counts but
    not *triple* counts
  * triples routed to the ``other`` long-tail bucket
  * triples dropped (code tokens) by the surface normalizer
  * top collapses (canonical ← which raw forms, by triple weight)

READ-ONLY / report-only — never mutates the graph.

Vocab source:
  * ``--freq-file FILE`` — JSON list of ``{"rt": str, "n": int}`` (produced by
    the curl below). Preferred — decouples the heavy GROUP BY from the report.
  * ``--live`` — run the GROUP BY directly over READ-ONLY ``/cypher``.

Capture frequencies live::

    set -a; source ~/.config/palace-daemon/env; set +a
    curl -sS -H "X-Api-Key: $PALACE_API_KEY" -H "Content-Type: application/json" \\
         "$PALACE_DAEMON_URL/cypher" \\
         -d '{"cypher":"MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"}' \\
      | jq '[.rows[] | select(.rt != null) | {rt:(.rt|tostring), n:.n}]' > freq.json
    python scripts/canonical_vocab_report.py --freq-file freq.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kg_canonical_vocab import CANONICAL_RELATIONS, CanonicalMapper  # noqa: E402
from kg_predicate_norm import normalize_predicate  # noqa: E402


def _load_freq(args: argparse.Namespace) -> list[dict]:
    if args.freq_file:
        try:
            with open(args.freq_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as e:
            raise SystemExit(f"cannot read freq file {args.freq_file}: {e}")
        except json.JSONDecodeError as e:
            raise SystemExit(f"{args.freq_file}: invalid JSON ({e})")
        if not isinstance(data, list):
            raise SystemExit(f"{args.freq_file}: expected a JSON list of {{rt,n}}")
        return data
    if args.live:
        return _fetch_live_freq()
    raise SystemExit("need --freq-file or --live")


_FREQ_CYPHER = (
    "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"
)


def _fetch_live_freq() -> list[dict]:
    """READ-ONLY GROUP BY of predicate frequencies via ``/cypher``."""
    import urllib.error
    import urllib.request

    key = os.environ.get("PALACE_API_KEY")
    url = os.environ.get("PALACE_DAEMON_URL")
    if not key or not url:
        raise SystemExit(
            "--live needs PALACE_API_KEY and PALACE_DAEMON_URL in the env"
        )
    payload = json.dumps({"cypher": _FREQ_CYPHER}).encode()
    req = urllib.request.Request(
        f"{url}/cypher", data=payload,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    timeout = float(os.environ.get("PALACE_LIVE_TIMEOUT", "600"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"--live: /cypher HTTP {e.code} ({e.reason})")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SystemExit(f"--live fetch failed (timeout={timeout}s): {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"--live: /cypher invalid JSON ({e})")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SystemExit("--live: /cypher response missing 'rows' list")
    out = []
    for r in rows:
        if isinstance(r, dict) and r.get("rt") is not None:
            out.append({"rt": str(r["rt"]), "n": int(r.get("n", 0))})
    return out


# Embedding cache: NOT pickle. ``vecs`` is a plain float32 array and ``terms``
# is stored as a single newline-joined UTF-8 blob, so the cache loads with
# allow_pickle=False (no arbitrary-code-execution surface even if the file is
# tampered with). The cache only short-circuits re-embedding; a mismatch falls
# back to recomputing.
def _load_embed_cache(path: Optional[str], terms: list[str]):
    """Return the cached query matrix if it matches ``terms``, else None."""
    if not path or not os.path.exists(path):
        return None
    try:
        import numpy as np

        with np.load(path, allow_pickle=False) as z:
            cached_terms = bytes(z["terms"].tobytes()).decode("utf-8").split("\n")
            mat = z["vecs"]
        if cached_terms == terms and mat.shape[0] == len(terms):
            return mat.astype("float32")
    except Exception:
        return None
    return None


def _save_embed_cache(path: Optional[str], terms: list[str], mat) -> None:
    if not path:
        return
    try:
        import numpy as np

        blob = np.frombuffer("\n".join(terms).encode("utf-8"), dtype="uint8")
        np.savez(path, terms=blob, vecs=mat)
    except Exception:
        pass


def _classify_all(
    freq: list[dict], mapper: CanonicalMapper
) -> list[tuple[str, int, Optional[str]]]:
    """Return [(raw, n, canonical_or_other_or_None)] for every entry.

    Batches the embedding work: surface-normalize every raw, embed the unique
    normalized forms in chunks, and do nearest-canonical via numpy matrix
    multiply — ~seconds for 64k vs ~15 min calling map_predicate one at a time.
    Falls back to the mapper's per-item path in lexical mode.
    """
    names = mapper._names
    name_set = set(names)

    # normalize once; bucket drops (None) immediately
    norm_of: dict[str, Optional[str]] = {}
    for e in freq:
        raw = e["rt"]
        norm_of[raw] = normalize_predicate(raw)

    if mapper.mode != "embedding":
        out = []
        for e in freq:
            canon, _ = mapper.map_predicate(e["rt"])
            out.append((e["rt"], int(e["n"]), canon))
        return out

    import numpy as np

    # unique normalized strings that need scoring (exact canonical hits and
    # drops are resolved without embedding)
    to_score = sorted({
        n for n in norm_of.values()
        if n is not None and n not in name_set
    })
    gloss_mat = np.asarray(mapper._gloss_vecs, dtype="float32")
    gloss_mat /= (np.linalg.norm(gloss_mat, axis=1, keepdims=True) + 1e-9)

    # Optional embedding cache. Embedding ~62k strings on CPU takes ~20 min, so
    # a threshold sweep would otherwise re-pay that each run. Cache the
    # query-vector matrix keyed by the sorted term list. The canonical glosses
    # are NOT cached (cheap, and they may be edited between runs).
    cache_path = os.environ.get("PALACE_PRED_EMBED_CACHE")
    qmat = _load_embed_cache(cache_path, to_score)
    if qmat is None:
        ef = mapper._ef
        rows = []
        CHUNK = 512
        for i in range(0, len(to_score), CHUNK):
            chunk = to_score[i:i + CHUNK]
            cv = np.asarray(ef(chunk), dtype="float32")
            cv /= (np.linalg.norm(cv, axis=1, keepdims=True) + 1e-9)
            rows.append(cv)
        qmat = np.vstack(rows) if rows else np.zeros((0, gloss_mat.shape[1]), "float32")
        _save_embed_cache(cache_path, to_score, qmat)

    best_canon: dict[str, tuple[str, float]] = {}
    sims_all = qmat @ gloss_mat.T  # (n_terms, n_canon)
    if len(to_score):
        idx_all = sims_all.argmax(axis=1)
        for j, term in enumerate(to_score):
            bi = int(idx_all[j])
            best_canon[term] = (names[bi], float(sims_all[j, bi]))

    out: list[tuple[str, int, Optional[str]]] = []
    for e in freq:
        raw = e["rt"]
        n = int(e["n"])
        nf = norm_of[raw]
        if nf is None:
            out.append((raw, n, None))
        elif nf in name_set:
            out.append((raw, n, nf))
        else:
            canon, score = best_canon[nf]
            out.append((raw, n, canon if score >= mapper.threshold else "other"))
    return out


def build_report(freq: list[dict], mapper: CanonicalMapper) -> dict:
    total_triples = sum(int(e["n"]) for e in freq)
    raw_distinct = len(freq)

    # triples per canonical, distinct raws per canonical, collapse provenance
    canon_triples: dict[str, int] = defaultdict(int)
    canon_distinct: dict[str, int] = defaultdict(int)
    collapse_examples: dict[str, list[tuple[str, int]]] = defaultdict(list)
    dropped_triples = 0
    dropped_distinct = 0
    other_triples = 0
    other_distinct = 0

    for raw, n, canon in _classify_all(freq, mapper):
        if canon is None:
            dropped_triples += n
            dropped_distinct += 1
            continue
        if canon == "other":
            other_triples += n
            other_distinct += 1
            continue
        canon_triples[canon] += n
        canon_distinct[canon] += 1
        collapse_examples[canon].append((raw, n))

    covered_triples = sum(canon_triples.values())
    mapped_cardinality = len(canon_triples)

    # top collapses by triple weight, with a few example raws each
    top = sorted(canon_triples.items(), key=lambda kv: -kv[1])
    collapses = {}
    for canon, _n in top:
        exs = sorted(collapse_examples[canon], key=lambda kv: -kv[1])[:8]
        collapses[canon] = {
            "triples": canon_triples[canon],
            "distinct_raws": canon_distinct[canon],
            "examples": [r for r, _ in exs],
        }

    return {
        "mode": mapper.mode,
        "threshold": mapper.threshold,
        "n_canonicals_defined": len(CANONICAL_RELATIONS),
        "total_triples": total_triples,
        "raw_distinct": raw_distinct,
        "mapped_cardinality": mapped_cardinality,
        "covered_triples": covered_triples,
        "covered_pct": 100.0 * covered_triples / total_triples if total_triples else 0.0,
        "other_triples": other_triples,
        "other_distinct": other_distinct,
        "other_pct": 100.0 * other_triples / total_triples if total_triples else 0.0,
        "dropped_triples": dropped_triples,
        "dropped_distinct": dropped_distinct,
        "dropped_pct": 100.0 * dropped_triples / total_triples if total_triples else 0.0,
        "collapses": collapses,
    }


def print_report(rep: dict) -> None:
    print("=" * 72)
    print("CLOSED-VOCABULARY PREDICATE MAPPING — DRY RUN (issue #72)")
    print("=" * 72)
    print(f"scorer mode            : {rep['mode']}"
          + ("  (NO embedding model — lexical fallback!)"
             if rep["mode"] == "lexical" else ""))
    print(f"distance threshold     : {rep['threshold']}")
    print(f"canonical relations    : {rep['n_canonicals_defined']} defined")
    print(f"total triples          : {rep['total_triples']:,}")
    print(f"raw distinct predicates: {rep['raw_distinct']:,}")
    print(f"mapped cardinality     : {rep['mapped_cardinality']} canonicals used"
          " (+ 'other' bucket)")
    print()
    print(f"TRIPLE COVERAGE by canonical : {rep['covered_pct']:.1f}%  "
          f"({rep['covered_triples']:,} triples)")
    print(f"  → 'other' (long tail)      : {rep['other_pct']:.1f}%  "
          f"({rep['other_triples']:,} triples, {rep['other_distinct']:,} distinct raws)")
    print(f"  → dropped (code tokens)    : {rep['dropped_pct']:.1f}%  "
          f"({rep['dropped_triples']:,} triples)")
    print()
    print("-- TOP CANONICAL COLLAPSES (by triple weight) " + "-" * 25)
    for canon, info in list(rep["collapses"].items())[:25]:
        exs = ", ".join(info["examples"][:6])
        print(f"  {canon:<14} {info['triples']:>9,} triples  "
              f"({info['distinct_raws']:,} raws)  ← {exs}")
    print()
    print("NOTE: read-only design spike. No production graph mutation performed.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--freq-file", help="JSON list of {rt,n}")
    ap.add_argument("--live", action="store_true", help="READ-ONLY /cypher GROUP BY")
    ap.add_argument("--threshold", type=float, default=0.45,
                    help="cosine threshold for canonical match (default 0.45)")
    ap.add_argument("--lexical", action="store_true",
                    help="force lexical fallback (skip embedding model)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    args = ap.parse_args(argv)

    freq = _load_freq(args)
    mapper = CanonicalMapper(threshold=args.threshold,
                             use_embeddings=not args.lexical)
    rep = build_report(freq, mapper)
    if args.json:
        print(json.dumps(rep, indent=2))
    else:
        print_report(rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
