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

## Apply mechanism

Set-based postgres UPDATE on the AGE backing table ``mempalace_kg."RELATION"``,
joining a TEMP ``predicate_mapping`` table of (raw, canonical) pairs. One seq
scan + set-based join rewrites every edge whose ``properties->>'relation_type'``
matches a mapping row AND that does not already have ``raw_relation_type`` set.

Proven on the production graph 2026-05-28: 1,467,937 edges in 23 seconds
(~63k edges/sec). Per-rule cypher ``MATCH ... SET`` was tried first and is
unworkable at this scale — no postgres index on ``properties->>'relation_type'``
means each per-rule MATCH is a full edge scan, and MVCC dead-tuple accumulation
from prior SETs makes successive scans progressively worse (per-batch time
doubled in the live test). The direct-SQL pattern bypasses both problems.

The exact rewrite is::

    UPDATE mempalace_kg."RELATION" e
    SET properties = (
        ((e.properties::text::jsonb)
         || jsonb_build_object(
              'raw_relation_type', (e.properties::text::jsonb)->>'relation_type',
              'relation_type', m.canonical)
        )::text::ag_catalog.agtype
    )
    FROM predicate_mapping m
    WHERE ((e.properties::text::jsonb)->>'relation_type') = m.raw
      AND NOT ((e.properties::text::jsonb) ? 'raw_relation_type')

(``ag_catalog.agtype`` must be fully qualified — the default ``search_path``
does not include ``ag_catalog``, and the cast otherwise fails.) The original
``relation_type`` is preserved in ``raw_relation_type`` for reversibility.

## Re-mapping already-migrated edges (``--remap-existing``, #45)

The first-migration UPDATE above is one-shot: its ``AND NOT (... ?
'raw_relation_type')`` guard makes a second run a no-op, because every migrated
edge now has ``raw_relation_type`` set. That guard is correct for the original
rollout but blocks the #45 use case — after the predicate *normalizer*
(``mempalace.kg_predicate_norm``) is improved, the bulk of the ``other`` bucket
should be re-evaluated, and those edges are exactly the already-migrated ones.

``--remap-existing`` switches both the plan and the apply:

* The frequency read uses ``coalesce(r.raw_relation_type, r.relation_type)`` so
  the mapper re-evaluates the ORIGINAL predicate, not the prior (possibly
  ``other``) canonical.
* The UPDATE keys the join on the same ``coalesce(...)`` original, DROPS the
  first-migration guard, PRESERVES the original (``coalesce`` keeps an existing
  ``raw_relation_type``, only backfilling it on never-migrated edges), and adds
  ``relation_type IS DISTINCT FROM m.canonical`` so only edges whose canonical
  actually changes are touched (no MVCC churn on no-ops)::

    UPDATE mempalace_kg."RELATION" e
    SET properties = (
        ((e.properties::text::jsonb)
         || jsonb_build_object(
              'raw_relation_type',
              COALESCE((e.properties::text::jsonb)->>'raw_relation_type',
                       (e.properties::text::jsonb)->>'relation_type'),
              'relation_type', m.canonical)
        )::text::ag_catalog.agtype
    )
    FROM predicate_mapping m
    WHERE COALESCE((e.properties::text::jsonb)->>'raw_relation_type',
                   (e.properties::text::jsonb)->>'relation_type') = m.raw
      AND ((e.properties::text::jsonb)->>'relation_type') IS DISTINCT FROM m.canonical

Default OFF — without the flag, behavior is byte-for-byte the original
first-migration semantics. Same backup + paused-worker gating applies.

## Reversibility

Rollback restores ``relation_type`` from ``raw_relation_type`` and drops the
latter::

    UPDATE mempalace_kg."RELATION" e
    SET properties = (
        ((e.properties::text::jsonb - 'raw_relation_type')
         || jsonb_build_object('relation_type',
                                (e.properties::text::jsonb)->>'raw_relation_type')
        )::text::ag_catalog.agtype
    )
    WHERE (e.properties::text::jsonb) ? 'raw_relation_type'

## Code tokens

Code-token predicates the canonical mapper drops are NOT deleted by this
migration (deletion is destructive and out of scope) — they are reported and
left as-is. ``--drop-code-tokens`` is reserved for a follow-up; it currently
prints a no-op warning under ``--apply``.

## Operational checklist (before --apply)

1. ``pg_dump -F c -n mempalace_kg -n ag_catalog <db> > backup.dump`` — mandatory.
2. ``sudo systemctl stop mempalace-kg-extract@<port>.service`` — pause concurrent
   RELATION writes so the seq scan sees a stable snapshot.
3. ``--apply --dsn $MEMPALACE_POSTGRES_DSN --i-have-a-backup`` — the two gates
   below refuse to mutate without both.
4. Resume the kg-extract worker after the UPDATE returns clean counts.
"""
from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict

from mempalace.kg_canonical_vocab import CanonicalMapper

_FREQ_CYPHER = (
    "MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"
)

# Remap-existing variant (#45): read the ORIGINAL predicate, not the stored
# canonical. Edges migrated by a prior run carry the original in
# ``raw_relation_type`` and a (possibly ``other``) canonical in
# ``relation_type``; to re-evaluate them against an improved mapper we must feed
# the mapper the original. ``coalesce`` falls back to ``relation_type`` for
# never-migrated edges (their stored value IS the original).
_FREQ_CYPHER_REMAP = (
    "MATCH ()-[r:RELATION]->() "
    "RETURN coalesce(r.raw_relation_type, r.relation_type) AS rt, count(*) AS n"
)


def _fetch_vocab_readonly(remap_existing: bool = False) -> list[dict]:
    """READ-ONLY GROUP BY of predicate frequencies via the daemon /cypher.

    When ``remap_existing`` is set, the frequency query reads the original
    predicate (``coalesce(raw_relation_type, relation_type)``) so the plan
    re-evaluates already-migrated edges against the current mapper (#45).
    """
    import urllib.error
    import urllib.request

    key = os.environ.get("PALACE_API_KEY")
    url = os.environ.get("PALACE_DAEMON_URL")
    if not key or not url:
        raise SystemExit(
            "need PALACE_API_KEY + PALACE_DAEMON_URL (source ~/.config/palace-daemon/env)"
        )
    cypher = _FREQ_CYPHER_REMAP if remap_existing else _FREQ_CYPHER
    payload = json.dumps({"cypher": cypher}).encode()
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
    return _fetch_vocab_readonly(remap_existing=getattr(args, "remap_existing", False))


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


def print_plan(plan: dict, mode: str, remap_existing: bool = False) -> None:
    te = plan["total_edges"]
    tag = " [REMAP-EXISTING]" if remap_existing else ""
    print("=" * 72)
    print(f"CANONICAL PREDICATE MIGRATION — {mode}{tag}  (issue #72a / #45)")
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
    if remap_existing:
        print("-- REMAP-EXISTING: re-evaluates already-migrated edges; keys on the "
              "ORIGINAL predicate (coalesce(raw_relation_type, relation_type)) --")
    if mode == "DRY-RUN":
        print("\nNO mutation performed. Re-run with --apply (host-side, with a "
              "backup) to migrate.")


def _apply(plan: dict, args: argparse.Namespace) -> None:
    """Run the set-based UPDATE on ``mempalace_kg."RELATION"`` (host-side only).

    Implementation pattern proven 2026-05-28 against the 1.76M-edge production
    graph: TEMP ``predicate_mapping`` populated via COPY, then one set-based
    UPDATE joining the backing table to the mapping. Earlier per-rule cypher
    ``MATCH ... SET`` was unworkable (no index on ``properties->>'relation_type'``,
    MVCC dead-tuple compounding); see module docstring for the postmortem.

    Goes via a DIRECT postgres DSN (``MEMPALACE_POSTGRES_DSN`` / ``--dsn``) —
    must run on the daemon host with kg-extract paused and a fresh ``pg_dump``
    in hand. Both guards below refuse if either condition is unmet.
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
            "(pg_dump -F c -n mempalace_kg -n ag_catalog <db>) and pause the "
            "kg-extract worker first. This mutates production."
        )

    remaps = plan.get("remaps") or []
    if not remaps:
        print("apply: no raw→canonical changes (graph already canonical)")
        return

    try:
        import psycopg
    except ImportError as e:
        raise SystemExit(f"--apply requires psycopg in the active venv: {e}")

    import time

    remap_existing = getattr(args, "remap_existing", False)
    mode_label = "REMAP-EXISTING" if remap_existing else "first-migration"
    print(f"apply [{mode_label}]: {len(remaps):,} rules → "
          f"~{plan['edges_would_change']:,} edge updates")
    print("  (set-based UPDATE on mempalace_kg.\"RELATION\" backing table)")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout = 0")

            cur.execute(
                'CREATE TEMP TABLE predicate_mapping ('
                '  raw text PRIMARY KEY, canonical text)'
            )
            with cur.copy("COPY predicate_mapping (raw, canonical) FROM STDIN") as cp:
                for r in remaps:
                    cp.write_row((r["raw"], r["canonical"]))
            cur.execute("ANALYZE predicate_mapping")

            t0 = time.time()
            if remap_existing:
                # #45: re-map ALREADY-migrated edges against an improved mapper.
                # Key the join on the ORIGINAL predicate
                # (coalesce(raw_relation_type, relation_type)) — NOT on the
                # stored relation_type, which for migrated edges is the prior
                # (possibly `other`) canonical. The original is PRESERVED:
                # coalesce keeps an existing raw_relation_type, only backfilling
                # it from relation_type on never-migrated edges. The
                # ``IS DISTINCT FROM`` guard skips no-op rewrites so we only pay
                # for edges whose canonical actually changes. NOTE the
                # first-migration guard (``NOT ( ? 'raw_relation_type')``) is
                # deliberately ABSENT here — that's the whole point of the flag.
                cur.execute(
                    """
                    UPDATE mempalace_kg."RELATION" e
                    SET properties = (
                        ((e.properties::text::jsonb)
                         || jsonb_build_object(
                              'raw_relation_type',
                              COALESCE(
                                  (e.properties::text::jsonb)->>'raw_relation_type',
                                  (e.properties::text::jsonb)->>'relation_type'),
                              'relation_type', m.canonical)
                        )::text::ag_catalog.agtype
                    )
                    FROM predicate_mapping m
                    WHERE COALESCE(
                              (e.properties::text::jsonb)->>'raw_relation_type',
                              (e.properties::text::jsonb)->>'relation_type') = m.raw
                      AND ((e.properties::text::jsonb)->>'relation_type')
                          IS DISTINCT FROM m.canonical
                    """
                )
            else:
                cur.execute(
                    """
                    UPDATE mempalace_kg."RELATION" e
                    SET properties = (
                        ((e.properties::text::jsonb)
                         || jsonb_build_object(
                              'raw_relation_type',
                              (e.properties::text::jsonb)->>'relation_type',
                              'relation_type', m.canonical)
                        )::text::ag_catalog.agtype
                    )
                    FROM predicate_mapping m
                    WHERE ((e.properties::text::jsonb)->>'relation_type') = m.raw
                      AND NOT ((e.properties::text::jsonb) ? 'raw_relation_type')
                    """
                )
            affected = cur.rowcount
            conn.commit()
            elapsed = time.time() - t0
            rate = affected / max(elapsed, 0.001)
            print(f"  UPDATE: {affected:,} edges in {elapsed:.0f}s ({rate:.0f} edges/s)")

            cur.execute(
                """
                SELECT count(*) FROM mempalace_kg."RELATION"
                WHERE NOT ((properties::text::jsonb) ? 'raw_relation_type')
                """
            )
            left = cur.fetchone()[0]
            expected_unmigrated = plan["edges_unchanged"] + plan["dropped_edges"]
            print(f"  remaining without raw_relation_type: {left:,} "
                  f"(expected ~{expected_unmigrated:,} = already-canonical + dropped)")

    if args.drop_code_tokens:
        print("  --drop-code-tokens: NOT YET IMPLEMENTED. Code-token edges remain; "
              "file a follow-up to add a DELETE pass.")


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
    ap.add_argument(
        "--remap-existing", action="store_true",
        help=(
            "RE-MAP already-migrated edges against the current mapper (#45). "
            "Reads the original predicate (coalesce(raw_relation_type, "
            "relation_type)) for the plan AND drops the first-migration guard "
            "in --apply, so edges that previously landed in `other` get "
            "re-evaluated. Default OFF: behavior is byte-for-byte the original "
            "first-migration semantics. Use after the predicate normalizer has "
            "been improved and you want the existing graph to reflect it."
        ),
    )
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
        out["remap_existing"] = bool(args.remap_existing)
        print(json.dumps(out, indent=2))
    else:
        print_plan(plan, "APPLY" if args.apply else "DRY-RUN",
                   remap_existing=args.remap_existing)

    if args.apply:
        _apply(plan, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
