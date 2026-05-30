#!/usr/bin/env python3
"""Determinism probe for the /search FlashRank rerank stage.

Question (from SME #117): a daemon restart reordered 49% of top-5 on
identical data. Is the rerank stage itself nondeterministic across a
process restart, or was the reorder caused by a candidate-set change?

This probes the rerank in isolation — no daemon, no prod, no network beyond
the one-time model fetch. Two checks:

  1. IN-PROCESS REPEAT: load the ranker once, rerank the same (query,
     passages) N times. Deterministic ⇒ byte-identical order + scores.
  2. FRESH-PROCESS RELOAD: re-exec this script in --worker mode, which
     loads a brand-new Ranker (simulating a daemon restart) and reranks the
     SAME fixed input. Diff the two processes' outputs. Deterministic ⇒
     identical order + identical scores to the float.

Exit 0 = deterministic across reload; nonzero = a real nondeterminism bug.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]  # scripts/evals/ -> repo root
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

QUERY = "What was my personal best time in the charity 5K run?"
# A fixed candidate set resembling a top-20 vector pool — mixed-relevance
# passages so the reranker has real reordering work to do.
PASSAGES = [
    "I'm training for a charity 5K and want to beat my personal best of 25:50.",
    "Do you have drills to improve my tennis toss consistency?",
    "My interval training focuses on maintaining a consistent pace.",
    "I incorporated strength training: squats, lunges, deadlifts.",
    "The 5K route this year goes through the riverside park.",
    "I set a new personal record of 24:32 at last weekend's 5K.",
    "Recovery runs should stay in zone 2 heart rate.",
    "I'm getting ready for a tennis tournament on May 6th.",
    "Hydration matters more in summer races than winter ones.",
    "My running shoes need replacing after 400 miles.",
    "Two-factor auth adds a second layer beyond your password.",
    "The charity raised $12,000 for the local food bank.",
    "Tempo runs build lactate threshold for faster 5K splits.",
    "I prefer morning workouts before the day gets busy.",
    "My best mile split during the 5K was 7:45.",
    "Carb-loading the night before a race helps endurance.",
    "I track my runs with a GPS watch and review the splits.",
    "Stretching after the run prevents next-day soreness.",
    "The weather on race day was cool and overcast — ideal.",
    "I want to qualify for the regional 10K next season.",
]


def _rerank_once() -> list[dict]:
    """Load a FRESH Ranker and rerank the fixed input once.

    Fresh load each call = simulates the daemon's first-call singleton load
    after a restart. Returns [{id, score}] in reranked order.
    """
    from flashrank import Ranker, RerankRequest
    import os
    ranker = Ranker(
        model_name=os.getenv("PALACE_RERANK_MODEL", "ms-marco-TinyBERT-L-2-v2"),
        max_length=int(os.getenv("PALACE_RERANK_MAX_LENGTH", "512")),
    )
    passages = [{"id": i, "text": t} for i, t in enumerate(PASSAGES)]
    scored = ranker.rerank(RerankRequest(query=QUERY, passages=passages))
    return [{"id": int(s["id"]), "score": float(s["score"])} for s in scored]


def _worker() -> int:
    """--worker mode: fresh process, fresh model load, emit JSON to stdout."""
    out = _rerank_once()
    print(json.dumps(out))
    return 0


def _fresh_process_run() -> list[dict]:
    """Re-exec this script in --worker mode and parse its reranked output."""
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker"],
        capture_output=True, text=True, timeout=300,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"worker failed rc={proc.returncode}: {proc.stderr[-500:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--worker" in argv:
        return _worker()

    print("=" * 70)
    print("FlashRank rerank determinism probe (palace-daemon /search stage)")
    print("=" * 70)

    # Check 1 — in-process repeat (load once, rerank 5×).
    from flashrank import Ranker, RerankRequest
    import os
    ranker = Ranker(
        model_name=os.getenv("PALACE_RERANK_MODEL", "ms-marco-TinyBERT-L-2-v2"),
        max_length=512,
    )
    runs = []
    for _ in range(5):
        passages = [{"id": i, "text": t} for i, t in enumerate(PASSAGES)]
        scored = ranker.rerank(RerankRequest(query=QUERY, passages=passages))
        runs.append([(int(s["id"]), round(float(s["score"]), 9)) for s in scored])
    in_proc_ok = all(r == runs[0] for r in runs)
    print(f"\n[1] IN-PROCESS REPEAT (5×, same loaded ranker): "
          f"{'DETERMINISTIC' if in_proc_ok else 'NONDETERMINISTIC'}")
    print(f"    top-5 order: {[i for i, _ in runs[0][:5]]}")

    # Check 2 — two fresh processes (each loads the model anew = restart sim).
    p1 = _fresh_process_run()
    p2 = _fresh_process_run()
    order1 = [h["id"] for h in p1]
    order2 = [h["id"] for h in p2]
    order_ok = order1 == order2
    scores_ok = all(
        abs(a["score"] - b["score"]) < 1e-9 for a, b in zip(p1, p2)
    )
    print(f"\n[2] FRESH-PROCESS RELOAD (×2, model reloaded each — restart sim):")
    print(f"    proc-A top-5: {order1[:5]}")
    print(f"    proc-B top-5: {order2[:5]}")
    print(f"    order identical: {order_ok}")
    print(f"    scores identical (<1e-9): {scores_ok}")

    overall = in_proc_ok and order_ok and scores_ok
    print("\n" + "=" * 70)
    print(f"VERDICT: rerank stage is "
          f"{'DETERMINISTIC across restart' if overall else 'NONDETERMINISTIC'}")
    print("=" * 70)
    return 0 if overall else 1


if __name__ == "__main__":
    sys.exit(main())
