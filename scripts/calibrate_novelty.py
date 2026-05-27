#!/usr/bin/env python3
"""Calibrate the gzip-NCD novelty_score distribution on the EXISTING corpus.

Issue #47: novelty scoring is live (POST /memory) but we don't know what the
empirical score distribution looks like. We can't wait days for fresh writes,
so this samples drawers already in the palace and computes each one's
novelty_score against the rolling window of its wing/room siblings — using the
SAME ``novelty.score_novelty`` function the daemon uses at write time.

Read-only: hits the daemon's MCP read tools (``mempalace_list_drawers`` to
discover groups + block membership, ``mempalace_get_drawer`` for full
content). Never writes to the palace.

Window semantics mirror production exactly. Production fetches the N most
recent drawers in the target's (wing, room) and scores the new content as the
minimum NCD across that window. We reproduce this with a *block* method:

  - Pull a contiguous block of (window + K) recent drawers for a (wing, room).
  - For each position i >= window in the block, treat drawer[i] as the
    "new write" and drawers[i-window:i] as its rolling window. That is
    precisely "the N drawers that existed just before this one was written"
    (list ordering approximates insertion order).

This amortizes to ~1 full-content fetch per scored sample instead of
~window+1, which matters on a ~375k-drawer corpus over SSH.

Fidelity note: production's ``compute_novelty_for_write`` reads window text
from drawer fields ``text``/``content``/``preview``, but
``mempalace_list_drawers`` actually returns ``content_preview`` (truncated to
~200 chars). So the *live* scorer currently sees an empty window and returns
novelty_score=1.0 for every write (a latent bug — see the findings report).
This script computes the true *full-content* distribution by default (what a
corrected scorer would see); pass --use-preview to reproduce the truncated
distribution that a field-name fix alone (without de-truncation) would yield.

Usage (run where the daemon is reachable; on familiar it's on localhost)::

    set -a; . ~/.config/palace-daemon/env; set +a
    python3 scripts/calibrate_novelty.py --groups 60 --per-group 8 --window 20 \
        --out docs/evals/novelty_calibration.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import urllib.request
from collections import Counter, defaultdict
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import novelty  # noqa: E402  the real scoring module


def _mcp_call(url: str, api_key: str, name: str, arguments: dict, timeout: int = 60) -> Any:
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }).encode("utf-8")
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp",
        data=payload,
        headers={"content-type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8")
    # The daemon can return non-JSON on failure (an HTML 502/504 page from a
    # proxy, an empty body), so parse defensively — a bad response should
    # yield an empty result, not crash the whole calibration run.
    try:
        outer = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    try:
        text = outer["result"]["content"][0]["text"]
        return json.loads(text)
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        return outer


def list_drawers(url: str, api_key: str, wing: str | None, room: str | None,
                 limit: int, offset: int) -> list[dict]:
    args: dict = {"limit": limit, "offset": offset}
    if wing is not None:
        args["wing"] = wing
    if room is not None:
        args["room"] = room
    res = _mcp_call(url, api_key, "mempalace_list_drawers", args)
    if isinstance(res, dict):
        return res.get("drawers") or res.get("results") or []
    return []


def corpus_total(url: str, api_key: str) -> int:
    probe = _mcp_call(url, api_key, "mempalace_list_drawers", {"limit": 1, "offset": 0})
    return probe.get("total", 0) if isinstance(probe, dict) else 0


def get_full_content(url: str, api_key: str, drawer_id: str, cache: dict) -> str:
    if drawer_id in cache:
        return cache[drawer_id]
    res = _mcp_call(url, api_key, "mempalace_get_drawer", {"drawer_id": drawer_id})
    content = ""
    if isinstance(res, dict):
        content = res.get("content") or res.get("text") or ""
    cache[drawer_id] = content
    return content


def discover_groups(url: str, api_key: str, n_groups: int, scan_pages: int,
                    page_size: int, rng: random.Random) -> list[tuple[str, str]]:
    """Sample distinct (wing, room) groups by walking random offsets."""
    total = corpus_total(url, api_key)
    if not total:
        return []
    max_offset = max(0, total - page_size)
    seen: Counter = Counter()
    for _ in range(scan_pages):
        offset = rng.randint(0, max_offset)
        for r in list_drawers(url, api_key, None, None, page_size, offset):
            seen[(r.get("wing") or "unknown", r.get("room") or "unknown")] += 1
    # Prefer groups that actually have enough members to form a window.
    groups = [g for g, _ in seen.most_common()]
    rng.shuffle(groups)
    return groups[:n_groups]


def score_group(url: str, api_key: str, wing: str, room: str, window: int,
                per_group: int, use_preview: bool, cache: dict) -> list[dict]:
    """Score up to `per_group` drawers in (wing, room) via the block method.

    Returns one record per scored drawer. Each drawer is scored against the
    `window` drawers immediately preceding it in list order.
    """
    block_n = window + per_group
    rows = list_drawers(url, api_key, wing, room, block_n, 0)
    if len(rows) <= window:
        return []  # not enough siblings to form even one full window

    # Pre-fetch full content for the whole block (or use previews).
    texts: list[str] = []
    for r in rows:
        if use_preview:
            texts.append(r.get("content_preview") or r.get("preview") or "")
        else:
            did = r.get("drawer_id")
            texts.append(get_full_content(url, api_key, did, cache) if did else "")

    records: list[dict] = []
    for i in range(window, len(rows)):
        content = texts[i]
        if not content or not content.strip():
            continue
        win = texts[i - window:i]
        info = novelty.score_novelty(content, win)
        records.append({
            "drawer_id": rows[i].get("drawer_id"),
            "wing": wing, "room": room,
            "novelty_score": info.get("novelty_score"),
            "window_size": info.get("window_size", 0),
            "status": info.get("status"),
        })
        if len(records) >= per_group:
            break
    return records


def ascii_histogram(scores: list[float], bins: int = 20, width: int = 50) -> str:
    if not scores:
        return "(no scores)"
    counts = [0] * bins
    for s in scores:
        # Clamp on both ends: NCD can slightly exceed 1.0, and a negative
        # score (shouldn't happen, but defensively) would otherwise wrap into
        # a high bin via Python's negative indexing.
        counts[max(0, min(bins - 1, int(s * bins)))] += 1
    peak = max(counts) or 1
    out = []
    for i, c in enumerate(counts):
        bar = "#" * int(round(c / peak * width))
        out.append(f"[{i/bins:.2f}-{(i+1)/bins:.2f}) {c:5d} |{bar}")
    return "\n".join(out)


def summarize(scores: list[float]) -> dict:
    if not scores:
        return {"n": 0}
    s = sorted(scores)
    n = len(s)

    def pct(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        return round(s[f] + (s[c] - s[f]) * (k - f), 4)

    mean = sum(s) / n
    var = sum((x - mean) ** 2 for x in s) / n
    return {
        "n": n, "min": round(s[0], 4),
        "p05": pct(0.05), "p10": pct(0.10), "p25": pct(0.25),
        "median": pct(0.50), "mean": round(mean, 4),
        "p75": pct(0.75), "p90": pct(0.90), "p95": pct(0.95),
        "max": round(s[-1], 4), "stdev": round(var ** 0.5, 4),
    }


def _hist_data(scores: list[float], bins: int) -> list[dict]:
    counts = [0] * bins
    for s in scores:
        counts[max(0, min(bins - 1, int(s * bins)))] += 1
    return [{"lo": round(i / bins, 3), "hi": round((i + 1) / bins, 3), "count": c}
            for i, c in enumerate(counts)]


# --------------------------------------------------------------------------
# Offline synthetic corpus
# --------------------------------------------------------------------------
# Used when the live daemon is unreachable (--offline-sample). Builds a
# representative multi-wing/room set whose redundancy patterns mirror what we
# observe in the real palace: a large share of near-duplicate auto-save /
# checkpoint drawers that share a boilerplate prefix but differ in a few
# fields (the low-novelty cluster), mixed with genuinely varied prose
# (the high-novelty cluster). Scored through the IDENTICAL block rolling-window
# path, so this exercises the real `novelty.score_novelty` end to end.

_WORD_BANK = (
    "palace daemon chromadb hnsw segment vector embedding rerank cosine "
    "distance novelty compression gzip ncd drawer wing room taxonomy "
    "postgres age cypher knowledge graph triple predicate mention search "
    "hybrid bm25 fusion latency throughput backfill miner watcher hook "
    "session diary checkpoint deployment systemd firewall vlan collectd "
    "kubernetes autoscale recipe vanilla quantum photon spectroscopy garden "
    "tide harbor lantern meridian glacier ember thicket cobalt verdant "
).split()


def _rand_prose(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(_WORD_BANK) for _ in range(n_words))


def build_synthetic_corpus(rng: random.Random, window: int,
                           per_group: int) -> dict[tuple[str, str], list[str]]:
    """Return {(wing, room): [contents...]} ordered oldest->newest per group.

    Each group has enough drawers (window + per_group) to score `per_group`
    of them. Groups are seeded with one of three redundancy archetypes so the
    aggregate distribution is interesting (and plausibly bimodal):

      - "checkpoint": near-duplicate auto-save lines sharing a fixed prefix,
        differing only in ids/counts/timestamps -> low novelty.
      - "iterative":  prose where each drawer reuses ~60% of the previous
        drawer's words -> medium-low novelty (incremental edits).
      - "varied":     independent random prose per drawer -> high novelty.
    """
    rooms = ["sessions", "decisions", "discoveries", "architecture",
             "problems", "planning", "references"]
    wings = ["storyvox", "palace_daemon", "realmwatch", "familiar",
             "lexicon", "ha", "claude_code_switcher"]
    archetypes = ["checkpoint", "checkpoint", "iterative", "varied"]  # weighted

    corpus: dict[tuple[str, str], list[str]] = {}
    n_drawers = window + per_group
    gi = 0
    for wing in wings:
        for room in rooms:
            arch = archetypes[gi % len(archetypes)]
            gi += 1
            contents: list[str] = []
            if arch == "checkpoint":
                prefix = (f"AUTO-SAVE checkpoint for wing={wing} room={room} "
                          f"agent=claude-code type=diary_entry hook=force "
                          f"the palace daemon filed this session automatically ")
                for k in range(n_drawers):
                    contents.append(
                        prefix + f"id={rng.randint(10**11, 10**12)} "
                        f"msgs={rng.randint(80, 320)} "
                        f"ts=2026-05-{rng.randint(10,27):02d}T{rng.randint(0,23):02d}:"
                        f"{rng.randint(0,59):02d} iteration {k}")
            elif arch == "iterative":
                prev = _rand_prose(rng, 60)
                contents.append(prev)
                for _ in range(n_drawers - 1):
                    words = prev.split()
                    keep = int(len(words) * 0.6)
                    new = words[:keep] + [rng.choice(_WORD_BANK)
                                          for _ in range(len(words) - keep)]
                    rng.shuffle(new)
                    prev = " ".join(new)
                    contents.append(prev)
            else:  # varied
                for _ in range(n_drawers):
                    contents.append(_rand_prose(rng, rng.randint(40, 90)))
            corpus[(wing, room)] = contents
    return corpus


def run_offline(args) -> dict:
    rng = random.Random(args.seed)
    corpus = build_synthetic_corpus(rng, args.window, args.per_group)
    records: list[dict] = []
    by_room: dict[str, list[float]] = defaultdict(list)
    by_wing: dict[str, list[float]] = defaultdict(list)
    for (wing, room), contents in corpus.items():
        scored_here = 0
        for i in range(args.window, len(contents)):
            content = contents[i]
            win = contents[i - args.window:i]
            info = novelty.score_novelty(content, win)
            records.append({
                "drawer_id": f"synthetic_{wing}_{room}_{i}",
                "wing": wing, "room": room,
                "novelty_score": info.get("novelty_score"),
                "window_size": info.get("window_size", 0),
                "status": info.get("status"),
            })
            if info.get("status") == "ok":
                by_room[room].append(info["novelty_score"])
                by_wing[wing].append(info["novelty_score"])
            scored_here += 1
            if scored_here >= args.per_group:
                break
    scored = [r["novelty_score"] for r in records
              if r["status"] == "ok" and r["novelty_score"] is not None]
    return _emit(args, scored, records, by_room, by_wing,
                 groups_sampled=len(corpus), groups_empty=0, full_fetches=0,
                 mode="offline-synthetic")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--groups", type=int, default=60, help="distinct (wing,room) groups to sample")
    ap.add_argument("--per-group", type=int, default=8, help="drawers scored per group")
    ap.add_argument("--window", type=int, default=20, help="rolling-window size (prod default 20)")
    ap.add_argument("--scan-pages", type=int, default=30, help="random pages to discover groups")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--seed", type=int, default=47)
    ap.add_argument("--use-preview", action="store_true",
                    help="score against truncated content_preview instead of full content")
    ap.add_argument("--url", default=os.getenv("PALACE_DAEMON_URL", "http://localhost:8085"))
    ap.add_argument("--api-key", default=os.getenv("PALACE_API_KEY", ""))
    ap.add_argument("--offline-sample", action="store_true",
                    help="run against a built-in synthetic corpus instead of the live "
                         "daemon (validates the pipeline when familiar:8085 is unreachable)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if args.offline_sample:
        print(f"# Offline synthetic mode (window={args.window}, per_group={args.per_group})",
              file=sys.stderr)
        run_offline(args)
        return 0

    if not args.api_key:
        print("ERROR: PALACE_API_KEY not set (source ~/.config/palace-daemon/env).\n"
              "       For an offline validation run, pass --offline-sample.", file=sys.stderr)
        return 2

    rng = random.Random(args.seed)
    print(f"# Discovering up to {args.groups} groups ({args.scan_pages} scan pages)...", file=sys.stderr)
    groups = discover_groups(args.url, args.api_key, args.groups, args.scan_pages,
                             args.page_size, rng)
    print(f"# {len(groups)} groups; scoring (block method, window={args.window})...", file=sys.stderr)

    cache: dict[str, str] = {}
    records: list[dict] = []
    by_room: dict[str, list[float]] = defaultdict(list)
    by_wing: dict[str, list[float]] = defaultdict(list)
    skipped_groups = 0
    for gi, (wing, room) in enumerate(groups):
        recs = score_group(args.url, args.api_key, wing, room, args.window,
                           args.per_group, args.use_preview, cache)
        if not recs:
            skipped_groups += 1
        for r in recs:
            records.append(r)
            if r["status"] == "ok" and r["novelty_score"] is not None:
                by_room[room].append(r["novelty_score"])
                by_wing[wing].append(r["novelty_score"])
        if (gi + 1) % 10 == 0:
            print(f"#   {gi+1}/{len(groups)} groups, {len(records)} scored", file=sys.stderr)

    scored = [r["novelty_score"] for r in records
              if r["status"] == "ok" and r["novelty_score"] is not None]
    _emit(args, scored, records, by_room, by_wing,
          groups_sampled=len(groups), groups_empty=skipped_groups,
          full_fetches=len(cache),
          mode="preview(truncated)" if args.use_preview else "full-content")
    return 0


def _emit(args, scored, records, by_room, by_wing, *, groups_sampled,
          groups_empty, full_fetches, mode) -> dict:
    """Print the human report and (optionally) write the JSON artifact."""
    overall = summarize(scored)
    print("\n========================= NOVELTY CALIBRATION =========================")
    print(f"mode: {mode}  window={args.window}  groups={groups_sampled} "
          f"(empty={groups_empty})  scored={len(scored)}  full-fetches={full_fetches}")
    print("\n--- Overall novelty_score distribution ---")
    print(json.dumps(overall, indent=2))
    print("\n--- ASCII histogram (20 bins, 0=duplicate .. 1=novel) ---")
    print(ascii_histogram(scored))

    print("\n--- Per-room (>=10 scored) ---")
    for room in sorted(by_room, key=lambda r: -len(by_room[r])):
        if len(by_room[room]) < 10:
            continue
        st = summarize(by_room[room])
        print(f"  {room:14s} n={st['n']:4d}  p10={st['p10']}  median={st['median']}  "
              f"p90={st['p90']}  mean={st['mean']}")

    result = {
        "config": {
            "mode": mode, "window": args.window, "per_group": args.per_group,
            "seed": args.seed, "use_preview": args.use_preview,
            "groups_sampled": groups_sampled, "groups_empty": groups_empty,
            "scored": len(scored), "full_content_fetches": full_fetches,
        },
        "overall": overall,
        "per_room": {r: summarize(v) for r, v in by_room.items() if len(v) >= 10},
        "per_wing": {w: summarize(v) for w, v in by_wing.items() if len(v) >= 10},
        "histogram_bins": _hist_data(scored, 20),
        "records": records,
    }
    if args.out:
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(result, f, indent=2)
        print(f"\n# Wrote {args.out}", file=sys.stderr)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
