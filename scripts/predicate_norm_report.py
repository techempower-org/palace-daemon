#!/usr/bin/env python3
"""Dry-run predicate-normalization report (issue #50).

Runs the live (or sampled) AGE predicate vocabulary through
``kg_predicate_norm.normalize_predicate`` and prints what *would* change.
This is a READ-ONLY / report-only tool — it never writes to the graph.

Vocabulary source, in order of preference:

  1. ``--vocab-file FILE`` — a JSON file containing either a bare list of
     predicate strings, or an object with a ``relationship_types`` /
     ``predicates`` key. Produce one live with a READ-ONLY Cypher query for
     the distinct ``r.relation_type`` property values, e.g.::

         set -a; source ~/.config/palace-daemon/env; set +a
         curl -sS -H "X-Api-Key: $PALACE_API_KEY" \\
              -H "Content-Type: application/json" "$PALACE_DAEMON_URL/cypher" \\
              -d '{"cypher":"MATCH ()-[r:RELATION]->() RETURN DISTINCT r.relation_type AS rt"}' \\
           | jq '[.rows[].rt | select(. != null) | tostring]' > vocab.json
         python scripts/predicate_norm_report.py --vocab-file vocab.json

  2. ``--live`` — enumerate the distinct ``r.relation_type`` predicate values
     over the daemon's READ-ONLY ``POST /cypher`` endpoint (the transaction is
     marked ``READ ONLY``; no mutation). Uses ``PALACE_API_KEY`` /
     ``PALACE_DAEMON_URL`` from the env. NB: the daemon's ``kg_stats`` /
     ``graph_stats`` tools only return the AGE edge *labels*
     (``RELATION`` / ``MENTIONS``), not the predicate-value vocabulary — the
     ~64k distinct predicate strings live in ``r.relation_type``, which only
     a Cypher walk exposes.

  3. (default) the bundled ``_SAMPLE_VOCAB`` below — the contamination
     examples enumerated in issue #50, so the report is demonstrable even
     when the daemon is offline.

The report shows:
  * original cardinality vs post-normalization cardinality
  * top synonym collapses (raw → canonical, by how many raws fold in)
  * the full list of dropped code-tokens
  * negation rewrites (raw → not_<base>)
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from mempalace.kg_predicate_norm import normalize_predicate


# Bundled fallback — the exact contamination examples from issue #50 plus a
# handful of clean predicates so the collapse/keep behavior is visible. This
# is NOT the live vocabulary; it exists so the report runs offline.
_SAMPLE_VOCAB: list[str] = [
    # class 1: code tokens
    "appendchild", "createelement", "executemany", "setattribute",
    "getelementbyid", "queryselector", "fetchall",
    # class 2: synonyms (should collapse)
    "is", "is_a", "is_a_part_of", "is_a_reference", "was_a",
    "is_an_instance_of", "instance_of", "belongs_to", "refers_to",
    "requires", "depends_on", "depends_upon",
    # class 3: negation / punctuation
    "don't_adapt", "aren't_merged", "'doesn't_appear'", "does_not_appear",
    # clean predicates (should pass through unchanged)
    "works_on", "created_by", "part_of", "contains", "references",
]


def _load_vocab(args: argparse.Namespace) -> tuple[list[str], str]:
    """Return (vocab_list, source_label)."""
    if args.vocab_file:
        try:
            with open(args.vocab_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except OSError as e:
            raise SystemExit(f"cannot read vocab file {args.vocab_file}: {e}")
        except json.JSONDecodeError as e:
            raise SystemExit(f"{args.vocab_file}: invalid JSON ({e})")

        if isinstance(data, dict):
            vocab = data.get("relationship_types") or data.get("predicates")
            if vocab is None:
                raise SystemExit(
                    f"{args.vocab_file}: no 'relationship_types' or "
                    "'predicates' key in object"
                )
        elif isinstance(data, list):
            vocab = data
        else:
            raise SystemExit(
                f"{args.vocab_file}: expected a JSON list or object, got "
                f"{type(data).__name__}"
            )
        if not isinstance(vocab, list):
            raise SystemExit(
                f"{args.vocab_file}: 'relationship_types'/'predicates' must be "
                f"a list, got {type(vocab).__name__}"
            )
        return [str(v) for v in vocab], f"file:{args.vocab_file}"

    if args.live:
        return _fetch_live(), "live:cypher(distinct r.relation_type)"

    return list(_SAMPLE_VOCAB), "bundled-sample (issue #50 examples)"


_DISTINCT_RT_CYPHER = (
    "MATCH ()-[r:RELATION]->() RETURN DISTINCT r.relation_type AS rt"
)


def _fetch_live() -> list[str]:
    """Enumerate distinct ``r.relation_type`` values via READ-ONLY ``/cypher``.

    The daemon's ``POST /cypher`` marks its transaction ``READ ONLY``, so this
    is a pure read — no graph mutation is possible. We deliberately do NOT use
    ``kg_stats`` / ``graph_stats``: those return only the AGE edge labels
    (``RELATION`` / ``MENTIONS``), whereas the predicate vocabulary this report
    normalizes lives in the ``r.relation_type`` property.
    """
    import urllib.error
    import urllib.request

    key = os.environ.get("PALACE_API_KEY")
    url = os.environ.get("PALACE_DAEMON_URL")
    if not key or not url:
        raise SystemExit(
            "--live needs PALACE_API_KEY and PALACE_DAEMON_URL in the env "
            "(source ~/.config/palace-daemon/env first)"
        )
    payload = json.dumps({"cypher": _DISTINCT_RT_CYPHER}).encode()
    req = urllib.request.Request(
        f"{url}/cypher", data=payload,
        headers={"X-Api-Key": key, "Content-Type": "application/json"},
    )
    # generous timeout: a DISTINCT walk over millions of RELATION edges is slow.
    # Overridable so CI / impatient callers aren't stuck on the default.
    timeout = float(os.environ.get("PALACE_LIVE_TIMEOUT", "600"))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(
            f"--live: /cypher returned HTTP {e.code} ({e.reason}). "
            "Note /cypher requires the daemon on the postgres backend."
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # URLError wraps most socket errors, but a bare read-timeout surfaces
        # as TimeoutError/socket.timeout (an OSError) and would otherwise
        # traceback. The DISTINCT walk over ~1.7M edges can exceed the timeout;
        # fall back to --vocab-file with a curl if so.
        raise SystemExit(
            f"--live fetch failed (daemon at {url}, timeout={timeout}s): {e}. "
            "For a very large graph, capture the vocab with a longer curl and "
            "use --vocab-file (see module docstring)."
        )
    except json.JSONDecodeError as e:
        raise SystemExit(f"--live: /cypher returned invalid JSON ({e})")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SystemExit(
            f"--live: /cypher response missing 'rows' list (got {type(rows).__name__})"
        )
    out: list[str] = []
    for row in rows:
        rt = row.get("rt") if isinstance(row, dict) else None
        if rt is not None:
            out.append(str(rt))
    return out


def build_report(vocab: list[str]) -> dict:
    """Run the vocab through normalize_predicate and bucket the outcomes."""
    uniq = sorted(set(vocab))
    dropped: list[str] = []
    negation_rewrites: list[tuple[str, str]] = []
    # canonical -> set of raws that collapse into it
    collapses: dict[str, set[str]] = defaultdict(set)
    canonical_set: set[str] = set()

    for raw in uniq:
        norm = normalize_predicate(raw)
        if norm is None:
            dropped.append(raw)
            continue
        canonical_set.add(norm)
        if norm.startswith("not_") and raw != norm:
            negation_rewrites.append((raw, norm))
        if raw != norm:
            collapses[norm].add(raw)

    # only report canonicals that absorbed >1 distinct raw (a real collapse)
    real_collapses = {
        canon: sorted(raws)
        for canon, raws in collapses.items()
        if raws
    }

    return {
        "original_cardinality": len(uniq),
        "post_norm_cardinality": len(canonical_set),
        "dropped_count": len(dropped),
        "dropped": sorted(dropped),
        "collapses": dict(sorted(
            real_collapses.items(), key=lambda kv: (-len(kv[1]), kv[0])
        )),
        "negation_rewrites": sorted(negation_rewrites),
    }


def print_report(report: dict, source: str) -> None:
    oc = report["original_cardinality"]
    pc = report["post_norm_cardinality"]
    reduction = (1 - pc / oc) * 100 if oc else 0.0
    print("=" * 70)
    print("KG PREDICATE NORMALIZATION — DRY RUN (issue #50)")
    print("=" * 70)
    print(f"vocabulary source : {source}")
    print(f"original predicates (distinct) : {oc}")
    print(f"post-norm predicates (distinct): {pc}")
    print(f"dropped (code tokens)          : {report['dropped_count']}")
    print(f"cardinality reduction          : {reduction:.1f}%")
    print()
    print("-- TOP COLLAPSES (raw forms → canonical) " + "-" * 28)
    if not report["collapses"]:
        print("  (none)")
    for canon, raws in report["collapses"].items():
        print(f"  {canon}  ←  {', '.join(raws)}")
    print()
    print("-- DROPPED CODE TOKENS " + "-" * 46)
    print("  " + (", ".join(report["dropped"]) or "(none)"))
    print()
    print("-- NEGATION REWRITES (raw → not_<base>) " + "-" * 29)
    if not report["negation_rewrites"]:
        print("  (none)")
    for raw, norm in report["negation_rewrites"]:
        print(f"  {raw}  →  {norm}")
    print()
    print("NOTE: dry-run only. No production graph mutation performed.")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vocab-file", help="JSON list or graph_stats object")
    ap.add_argument("--live", action="store_true",
                    help="fetch live graph_stats (READ-ONLY)")
    ap.add_argument("--json", action="store_true",
                    help="emit the report as JSON instead of text")
    args = ap.parse_args(argv)

    vocab, source = _load_vocab(args)
    report = build_report(vocab)
    if args.json:
        print(json.dumps({"source": source, **report}, indent=2))
    else:
        print_report(report, source)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
