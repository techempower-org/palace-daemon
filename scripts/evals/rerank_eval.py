#!/usr/bin/env python3
"""Before/after eval probe for FlashRank cross-encoder reranking (issue #46).

Quantifies the retrieval-quality lift of the live rerank pass by comparing
two orderings of the *same* candidate pool:

  baseline  — the daemon's pre-rerank order (vector/hybrid distance asc)
  reranked  — the order FlashRank produced (rerank_score desc)

Both come out of a single read-only ``GET /search`` call per query: the
response already carries each hit's ``effective_distance`` (so baseline is
recoverable by re-sorting) and the post-rerank list order (reranked). The
``rerank`` trace block carries the per-request latency. Nothing is written
to the palace.

Relevance labels live in ``rerank_eval_queries.json`` as structural
predicates (source_file glob + content substring), hand-verified against
the production palace. A candidate is relevant iff it matches the predicate.

Metrics, per ordering:
  R@5 / R@10 — fraction of queries with >=1 relevant hit in the top K
  MRR        — mean reciprocal rank of the first relevant hit

Usage::

    venv/bin/python scripts/evals/rerank_eval.py \
        --url http://familiar:8085 --pool 20 \
        --out docs/evals/rerank-eval-2026-05-27.json

If ``--url`` is unreachable the script can instead rerank an in-process
candidate set (``--mode in-process``), driving rerank.py directly against
whatever candidates the local daemon URL returned with rerank disabled —
but the default and recommended mode is ``live`` against the deployed
daemon, which is the configuration under evaluation.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import requests

_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent  # palace-daemon repo root
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

DEFAULT_QUERIES = _HERE / "rerank_eval_queries.json"


def _is_retryable_http(e: requests.HTTPError) -> bool:
    """True for transient 5xx server errors; False for permanent 4xx.

    The contended daemon can return 502/503/504 while a restart settles —
    worth retrying. A 4xx (401 bad key, 400 bad query) won't fix itself.
    """
    resp = getattr(e, "response", None)
    code = getattr(resp, "status_code", None)
    return isinstance(code, int) and 500 <= code < 600


def _load_env_file(path: Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file (no shell expansion)."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _hit_text(hit: dict) -> str:
    for key in ("text", "document"):
        v = hit.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def is_relevant(hit: dict, rel: dict) -> bool:
    """Apply a query's structural relevance predicate to a single hit."""
    glob = rel.get("source_glob")
    if glob:
        src = hit.get("source_file") or ""
        # Simple suffix/substring match — globs here are filenames.
        if glob not in src:
            return False
    needles = rel.get("content_any") or []
    if not needles:
        # source_glob alone is sufficient if no content needle given.
        return bool(glob)
    text = _hit_text(hit)
    return any(n in text for n in needles)


def first_relevant_rank(ordering: list[dict], rel: dict) -> int | None:
    """1-based rank of the first relevant hit, or None if absent."""
    for i, h in enumerate(ordering, start=1):
        if is_relevant(h, rel):
            return i
    return None


def recall_at_k(ordering: list[dict], rel: dict, k: int) -> int:
    """1 if any relevant hit appears in the top-k, else 0 (per-query)."""
    rank = first_relevant_rank(ordering[:k], rel)
    return 1 if rank is not None else 0


def baseline_order(hits: list[dict]) -> list[dict]:
    """Reconstruct the pre-rerank order: ascending retrieval distance.

    Falls back to ``-similarity`` then original index so the sort is total
    and stable even on hits missing one of the fields.
    """
    def key(h: dict) -> tuple[float, float]:
        dist = h.get("effective_distance")
        if dist is None:
            dist = h.get("distance")
        if dist is None:
            sim = h.get("similarity")
            dist = (1.0 - sim) if isinstance(sim, (int, float)) else 9.99
        return (float(dist), -float(h.get("similarity") or 0.0))

    return sorted(hits, key=key)


def reranked_order(hits: list[dict]) -> list[dict]:
    """The order the daemon returned, i.e. rerank_score desc.

    The /search response already returns hits in reranked order, but we
    re-sort defensively by rerank_score so the harness is correct even if
    a caller hands us a shuffled list. Hits without a score sink to the
    tail in their existing order (mirrors rerank.py's unrankable handling).
    """
    scored = [h for h in hits if isinstance(h.get("rerank_score"), (int, float))]
    unscored = [h for h in hits if not isinstance(h.get("rerank_score"), (int, float))]
    scored.sort(key=lambda h: float(h["rerank_score"]), reverse=True)
    return scored + unscored


def fetch_live(
    url: str,
    api_key: str,
    query: str,
    pool: int,
    timeout: float,
    retries: int = 2,
    backoff: float = 3.0,
) -> dict:
    """One read-only GET /search call; returns the parsed JSON response.

    Retries on transient failures — timeouts, connection errors, and 5xx
    server responses. The production daemon shares a single mempalace writer
    and gets contended under concurrent load (it OOM-restarted mid-eval on
    2026-05-27), so a timeout or a 502/503/504 is not a verdict on the data.
    A 4xx (bad auth, bad request) is permanent and propagates immediately.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = requests.get(
                f"{url.rstrip('/')}/search",
                params={"q": query, "limit": pool},
                headers={"X-API-Key": api_key},
                timeout=timeout,
            )
            r.raise_for_status()
            return r.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            if attempt < retries:
                time.sleep(backoff * (attempt + 1))
        except requests.HTTPError as e:
            # Only 5xx is transient; 4xx is a permanent client error — don't
            # waste retries masking bad auth / a malformed request.
            if not _is_retryable_http(e) or attempt >= retries:
                raise
            last = e
            time.sleep(backoff * (attempt + 1))
    raise last  # type: ignore[misc]


def candidates_from_file(path: Path) -> dict[str, list[dict]]:
    """Load a pre-fetched candidate pool: ``{query_id: [hit, ...]}``.

    Each hit must carry ``text`` (or ``document``) and a retrieval distance
    (``effective_distance``/``distance``/``similarity``). This is how the
    harness runs when the live HTTP daemon is contended: candidates are
    pulled once (read-only, via the mempalace_search MCP tool against the
    same production palace) and frozen here, then reranked in-process with
    the *same* rerank.py the daemon uses — so the A/B is identical to live.
    """
    raw = json.loads(path.read_text())
    return raw.get("candidates", raw)


def rerank_in_process(query: str, hits: list[dict]) -> tuple[list[dict], dict]:
    """Drive rerank.py directly (production codepath) on a candidate list.

    Returns ``(reranked_hits, trace)`` mirroring what the daemon attaches.
    Imported lazily so ``--mode live`` doesn't pay the flashrank import.
    """
    import rerank  # daemon-root module
    os.environ["PALACE_RERANK_ENABLED"] = "true"
    return rerank.rerank_hits(query, [dict(h) for h in hits])


def evaluate(
    queries: list[dict],
    *,
    mode: str,
    url: str = "",
    api_key: str = "",
    pool: int = 20,
    timeout: float = 30.0,
    retries: int = 2,
    delay: float = 1.0,
    candidates: dict[str, list[dict]] | None = None,
) -> dict:
    per_query: list[dict] = []
    latencies: list[float] = []
    base_mrr = rer_mrr = 0.0
    base_r5 = rer_r5 = base_r10 = rer_r10 = 0
    usable = 0
    candidates = candidates or {}

    for qi, q in enumerate(queries):
        rel = q["relevant"]

        if mode == "candidates":
            hits = candidates.get(q["id"])
            if hits is None:
                per_query.append({"id": q["id"], "error": "no candidates in file for this id"})
                continue
            base = baseline_order(hits)
            rer, trace = rerank_in_process(q["query"], hits)
            lat = trace.get("latency_ms")
        else:  # live
            if qi and delay:
                time.sleep(delay)
            try:
                resp = fetch_live(url, api_key, q["query"], pool, timeout, retries=retries)
            except Exception as e:
                per_query.append({"id": q["id"], "error": f"{type(e).__name__}: {e}"})
                continue
            hits = resp.get("results") or []
            trace = resp.get("rerank") or {}
            lat = trace.get("latency_ms")
            base = baseline_order(hits)
            rer = reranked_order(hits)

        if isinstance(lat, (int, float)):
            latencies.append(float(lat))

        b_rank = first_relevant_rank(base, rel)
        r_rank = first_relevant_rank(rer, rel)
        n_rel = sum(1 for h in hits if is_relevant(h, rel))

        if n_rel == 0:
            # No relevant doc in the candidate pool — the query can't
            # discriminate the two orderings; exclude from aggregates but
            # record it so the report is honest about coverage.
            per_query.append({
                "id": q["id"],
                "query": q["query"],
                "n_candidates": len(hits),
                "n_relevant_in_pool": 0,
                "excluded": "no relevant candidate retrieved",
                "rerank_status": trace.get("status"),
            })
            continue

        usable += 1
        b5, r5 = recall_at_k(base, rel, 5), recall_at_k(rer, rel, 5)
        b10, r10 = recall_at_k(base, rel, 10), recall_at_k(rer, rel, 10)
        b_rr = 1.0 / b_rank if b_rank else 0.0
        r_rr = 1.0 / r_rank if r_rank else 0.0

        base_r5 += b5; rer_r5 += r5
        base_r10 += b10; rer_r10 += r10
        base_mrr += b_rr; rer_mrr += r_rr

        per_query.append({
            "id": q["id"],
            "query": q["query"],
            "n_candidates": len(hits),
            "n_relevant_in_pool": n_rel,
            "baseline_rank": b_rank,
            "reranked_rank": r_rank,
            "rank_delta": (b_rank - r_rank) if (b_rank and r_rank) else None,
            "rerank_status": trace.get("status"),
            "rerank_latency_ms": lat,
        })

    n = usable or 1
    summary = {
        "n_queries_total": len(queries),
        "n_queries_usable": usable,
        "n_excluded_no_relevant": sum(1 for p in per_query if p.get("excluded")),
        "n_errors": sum(1 for p in per_query if p.get("error")),
        "baseline": {
            "R@5": round(base_r5 / n, 4),
            "R@10": round(base_r10 / n, 4),
            "MRR": round(base_mrr / n, 4),
        },
        "reranked": {
            "R@5": round(rer_r5 / n, 4),
            "R@10": round(rer_r10 / n, 4),
            "MRR": round(rer_mrr / n, 4),
        },
        "latency_ms": {
            "n": len(latencies),
            "mean": round(sum(latencies) / len(latencies), 2) if latencies else None,
            "min": round(min(latencies), 2) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
        },
    }
    b, r = summary["baseline"], summary["reranked"]
    summary["delta"] = {
        "R@5": round(r["R@5"] - b["R@5"], 4),
        "R@10": round(r["R@10"] - b["R@10"], 4),
        "MRR": round(r["MRR"] - b["MRR"], 4),
        "MRR_pct": round(((r["MRR"] - b["MRR"]) / b["MRR"] * 100.0), 1) if b["MRR"] else None,
    }
    return {"summary": summary, "per_query": per_query}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="Daemon base URL (default: PALACE_DAEMON_URL or http://familiar:8085)")
    ap.add_argument("--api-key", default=None, help="API key (default: PALACE_API_KEY or ~/.config/palace-daemon/env)")
    ap.add_argument("--queries", default=str(DEFAULT_QUERIES), help="Path to the labeled query JSON")
    ap.add_argument("--mode", choices=("live", "candidates"), default="live",
                    help="live: hit the deployed daemon /search (default). candidates: rerank a pre-fetched pool in-process via rerank.py (use when the daemon is contended).")
    ap.add_argument("--candidates", default=None, help="Path to a frozen candidate-pool JSON (required for --mode candidates)")
    ap.add_argument("--pool", type=int, default=20, help="Candidate pool size per query (search limit, live mode)")
    ap.add_argument("--timeout", type=float, default=30.0, help="Per-request HTTP timeout (s)")
    ap.add_argument("--retries", type=int, default=2, help="Retry count per query on timeout/conn error")
    ap.add_argument("--delay", type=float, default=1.0, help="Polite delay between queries (s) to ease daemon contention")
    ap.add_argument("--out", default=None, help="Write the full result JSON here")
    args = ap.parse_args()

    env = _load_env_file(Path.home() / ".config" / "palace-daemon" / "env")
    url = args.url or os.getenv("PALACE_DAEMON_URL") or env.get("PALACE_DAEMON_URL") or "http://familiar:8085"
    api_key = args.api_key or os.getenv("PALACE_API_KEY") or env.get("PALACE_API_KEY")

    spec = json.loads(Path(args.queries).read_text())
    queries = spec["queries"]

    cand: dict[str, list[dict]] | None = None
    if args.mode == "candidates":
        if not args.candidates:
            print("ERROR: --mode candidates requires --candidates <file>", file=sys.stderr)
            return 2
        cand = candidates_from_file(Path(args.candidates))
        print(f"# rerank eval — mode=candidates, {len(queries)} queries, pool from {args.candidates}")
    else:
        if not api_key:
            print("ERROR: live mode needs an API key — set PALACE_API_KEY or pass --api-key", file=sys.stderr)
            return 2
        print(f"# rerank eval — mode=live, {len(queries)} queries, pool={args.pool}, url={url}")

    t0 = time.monotonic()
    result = evaluate(
        queries, mode=args.mode, url=url, api_key=api_key or "", pool=args.pool,
        timeout=args.timeout, retries=args.retries, delay=args.delay, candidates=cand,
    )
    result["meta"] = {
        "mode": args.mode,
        "url": url if args.mode == "live" else None,
        "candidates_file": str(Path(args.candidates).resolve()) if args.candidates else None,
        "pool": args.pool,
        "queries_file": str(Path(args.queries).resolve()),
        "wall_seconds": round(time.monotonic() - t0, 2),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

    s = result["summary"]
    b, r, d = s["baseline"], s["reranked"], s["delta"]
    print(f"\nusable queries: {s['n_queries_usable']}/{s['n_queries_total']}"
          f"  (excluded={s['n_excluded_no_relevant']}, errors={s['n_errors']})")
    print(f"{'metric':<8}{'baseline':>12}{'reranked':>12}{'delta':>12}")
    for m in ("R@5", "R@10", "MRR"):
        print(f"{m:<8}{b[m]:>12}{r[m]:>12}{d[m]:>+12}")
    lat = s["latency_ms"]
    if lat["mean"] is not None:
        print(f"\nrerank latency ms: mean={lat['mean']} min={lat['min']} max={lat['max']} (n={lat['n']})")

    if args.out:
        outp = Path(args.out)
        outp.parent.mkdir(parents=True, exist_ok=True)
        outp.write_text(json.dumps(result, indent=2))
        print(f"\nwrote {outp}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
