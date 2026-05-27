#!/usr/bin/env python3
"""One-shot canonical-predicate migration for existing RELATION edges (#72a).

Rewrites every existing RELATION edge's ``relation_type`` to its canonical form
(via :mod:`kg_canonical_vocab`), retaining the original as ``raw_relation_type``
for full reversibility. **Defaults to DRY-RUN.** ``--apply`` is gated and must
not be run without a graph backup + explicit go (see safety note below).

## Read vs write paths

* **Dry-run (default)** reads the live vocabulary with frequencies over the
  daemon's READ-ONLY ``POST /cypher`` endpoint (transaction marked READ ONLY),
  maps each predicate, and reports the would-change numbers. Fully safe.
* **``--apply``** mutates the graph. The daemon's ``/cypher`` is read-only by
  construction (it rejects write verbs with HTTP 403), so apply cannot go
  through the HTTP surface. It requires a **direct postgres connection**
  (``MEMPALACE_POSTGRES_DSN`` / ``--dsn``) — i.e. run it *on the daemon host*
  with the single-writer daemon paused and a fresh backup. This is intentional
  friction: bulk graph mutation should never originate from a remote read-only
  client.

## Reversibility

Apply does, per distinct raw predicate ``R`` with canonical ``C != R``::

    MATCH ()-[r:RELATION]->() WHERE r.relation_type = R
      SET r.raw_relation_type = R, r.relation_type = C

The original is preserved in ``raw_relation_type``, so a rollback is::

    MATCH ()-[r:RELATION]->() WHERE r.raw_relation_type IS NOT NULL
      SET r.relation_type = r.raw_relation_type REMOVE r.raw_relation_type

Code-token predicates the spike would *drop* are NOT deleted by this migration
(deletion is destructive and out of scope) — they are reported and left as-is
unless ``--drop-code-tokens`` is passed under ``--apply``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from kg_canonical_vocab import CanonicalMapper  # noqa: E402

_FREQ_CYPHER = (
    "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"
)


def _fetch_vocab_readonly() -> list[dict]:
    """READ-ONLY GROUP BY of predicate frequencies via the daemon /cypher."""
    import urllib.error
    import urllib.request

    key = os.environ.get("PALACE_API_KEY")
    url = os.environ.get("PALACE_DAEMON_URL")
    if not key or not url:
        raise SystemExit(
            "need PALACE_API_KEY + PALACE_DAEMON_URL (source ~/.config/palace-daemon/env)"
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
        raise SystemExit(f"/cypher HTTP {e.code} ({e.reason})")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise SystemExit(f"/cypher fetch failed (timeout={timeout}s): {e}")
    except json.JSONDecodeError as e:
        raise SystemExit(f"/cypher invalid JSON ({e})")
    rows = data.get("rows")
    if not isinstance(rows, list):
        raise SystemExit("/cypher response missing 'rows' list")
    return [
        {"rt": str(r["rt"]), "n": int(r.get("n", 0))}
        for r in rows
        if isinstance(r, dict) and r.get("rt") is not None
    ]


def _load_vocab(args: argparse.Namespace) -> list[dict]:
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
    return _fetch_vocab_readonly()


def build_plan(vocab: list[dict], mapper: CanonicalMapper) -> dict:
    """Compute the rewrite plan: per raw predicate, its canonical + edge count."""
    total_edges = sum(int(e["n"]) for e in vocab)
    raw_distinct = len(vocab)

    remaps: list[dict] = []          # raw → canonical (changed)
    unchanged_distinct = 0
    unchanged_edges = 0
    changed_edges = 0
    dropped_edges = 0
    dropped_distinct = 0
    other_edges = 0
    other_distinct = 0
    after_distinct: set[str] = set()
    canon_edges: dict[str, int] = defaultdict(int)

    for e in vocab:
        raw = e["rt"]
        n = int(e["n"])
        canon, _score = mapper.map_predicate(raw)
        if canon is None:
            dropped_edges += n
            dropped_distinct += 1
            continue
        after_distinct.add(canon)
        canon_edges[canon] += n
        if canon == "other":
            other_edges += n
            other_distinct += 1
        if canon == raw:
            unchanged_distinct += 1
            unchanged_edges += n
        else:
            changed_edges += n
            remaps.append({"raw": raw, "canonical": canon, "edges": n})

    remaps.sort(key=lambda d: -d["edges"])
    top_canon = sorted(canon_edges.items(), key=lambda kv: -kv[1])[:25]

    return {
        "total_edges": total_edges,
        "raw_distinct": raw_distinct,
        "after_distinct": len(after_distinct),
        "edges_would_change": changed_edges,
        "edges_would_change_pct": 100.0 * changed_edges / total_edges if total_edges else 0.0,
        "edges_unchanged": unchanged_edges,
        "distinct_remapped": len(remaps),
        "other_edges": other_edges,
        "other_distinct": other_distinct,
        "dropped_edges": dropped_edges,
        "dropped_distinct": dropped_distinct,
        "top_canonicals": [{"canonical": c, "edges": n} for c, n in top_canon],
        "top_remaps": remaps[:30],
        "remaps": remaps,  # full list used by --apply
    }


def print_plan(plan: dict, mode: str) -> None:
    te = plan["total_edges"]
    print("=" * 72)
    print(f"CANONICAL PREDICATE MIGRATION — {mode}  (issue #72a)")
    print("=" * 72)
    print(f"total RELATION edges     : {te:,}")
    print(f"distinct predicates now  : {plan['raw_distinct']:,}")
    print(f"distinct after migration : {plan['after_distinct']:,}")
    print(f"edges WOULD CHANGE       : {plan['edges_would_change']:,} "
          f"({plan['edges_would_change_pct']:.1f}%)")
    print(f"  via distinct remaps    : {plan['distinct_remapped']:,} raw→canonical rules")
    print(f"edges unchanged          : {plan['edges_unchanged']:,}")
    print(f"→ 'other' bucket         : {plan['other_edges']:,} edges "
          f"({plan['other_distinct']:,} distinct raws)")
    print(f"→ code tokens (NOT touched unless --drop-code-tokens): "
          f"{plan['dropped_edges']:,} edges ({plan['dropped_distinct']:,} distinct)")
    print()
    print("-- TOP REMAPS (raw → canonical, by edge count) " + "-" * 23)
    for r in plan["top_remaps"]:
        print(f"  {r['edges']:>9,}  {r['raw']}  →  {r['canonical']}")
    print()
    print("-- reversibility: original retained as r.raw_relation_type "
          "(rollback in module docstring) --")
    if mode == "DRY-RUN":
        print("\nNO mutation performed. Re-run with --apply (host-side, with a "
              "backup) to migrate.")


def _apply(plan: dict, args: argparse.Namespace) -> None:
    """Perform the rewrite via a DIRECT postgres connection (host-side only).

    Deliberately does NOT go through the daemon /cypher (read-only). Requires
    MEMPALACE_POSTGRES_DSN or --dsn. This path is gated and must only be run on
    the daemon host with a backup + the single-writer daemon paused.
    """
    dsn = args.dsn or os.environ.get("MEMPALACE_POSTGRES_DSN")
    if not dsn:
        raise SystemExit(
            "--apply needs --dsn or MEMPALACE_POSTGRES_DSN (host-side postgres). "
            "It cannot run through the read-only /cypher HTTP endpoint."
        )
    if not args.i_have_a_backup:
        raise SystemExit(
            "REFUSING --apply without --i-have-a-backup. Snapshot the AGE graph "
            "and pause the single-writer daemon first. This mutates production."
        )
    raise SystemExit(
        "apply path intentionally not auto-executed in this PR. The rewrite "
        "rules are in the dry-run report (plan['remaps']); JP + backup gate the "
        "real run. See module docstring for the exact SET/REMOVE Cypher."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--freq-file", help="JSON [{rt,n}] (skip the live read)")
    ap.add_argument("--threshold", type=float, default=0.45)
    ap.add_argument("--lexical", action="store_true",
                    help="lexical fallback (no embedding model)")
    ap.add_argument("--apply", action="store_true",
                    help="MUTATE the graph (host-side postgres; gated)")
    ap.add_argument("--dsn", help="postgres DSN for --apply")
    ap.add_argument("--i-have-a-backup", action="store_true",
                    help="required acknowledgement for --apply")
    ap.add_argument("--drop-code-tokens", action="store_true",
                    help="(apply only) also delete code-token edges")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    vocab = _load_vocab(args)
    mapper = CanonicalMapper(threshold=args.threshold,
                             use_embeddings=not args.lexical)
    plan = build_plan(vocab, mapper)

    if args.json:
        # don't dump the giant full remap list in the summary json
        out = {k: v for k, v in plan.items() if k != "remaps"}
        out["mode"] = "APPLY" if args.apply else "DRY-RUN"
        print(json.dumps(out, indent=2))
    else:
        print_plan(plan, "APPLY" if args.apply else "DRY-RUN")

    if args.apply:
        _apply(plan, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
