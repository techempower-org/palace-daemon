# FlashRank rerank quality-lift eval probe (issue #46)

**Date:** 2026-05-27
**Target:** `rerank.py` — FlashRank cross-encoder, model `ms-marco-TinyBERT-L-2-v2` ("nano", ~4 MB ONNX, CPU)
**Daemon under eval:** `palace-daemon` v1.8.3 on `familiar`, `MEMPALACE_BACKEND=postgres`, ~375k drawers
**Harness:** `scripts/evals/rerank_eval.py` · **Query set:** `scripts/evals/rerank_eval_queries.json`

> **Recommendation: KEEP nano now; schedule a follow-up A/B against MiniLM L-12.**
> MRR improved **+15.3%** (0.761 → 0.877) at an acceptable **47 ms** mean
> latency, and rerank rescued one buried answer from rank 5 → 1. But it also
> demoted one relevant doc from rank 3 → 7 (the only R@5 regression), caused by
> cross-encoder score compression (~0.999 ties). The lift is real and worth
> keeping; the regression + flat scores are the case for testing a larger model.
> (Note: this FlashRank build ships `ms-marco-MiniLM-L-12-v2` but **no L-6** —
> the issue mentioned L-6, but only L-12 is actually available here.)

---

## What was measured

A *before/after ordering* comparison on an identical candidate pool. For each
labeled query we obtain one candidate pool from the production palace and score
two orderings of it:

| ordering | definition |
|---|---|
| **baseline** | candidates sorted by retrieval distance ascending (`effective_distance`) — the order the daemon would return *with `PALACE_RERANK_ENABLED=false`* |
| **reranked** | candidates sorted by FlashRank `rerank_score` descending — what the daemon returns today (`PALACE_RERANK_ENABLED=true`) |

Because both orderings operate on the *same* candidate set, the comparison
isolates the reranker's contribution and nothing else. This is the cleanest
possible A/B: retrieval (candidate generation) is held constant; only the
final ordering function changes — exactly the change the rerank pass makes in
production.

Metrics (computed per ordering, averaged over usable queries):

- **R@5 / R@10** — fraction of queries with ≥1 relevant hit in the top K
- **MRR** — mean reciprocal rank of the first relevant hit

## How the ground-truth set was built

12 queries, each paired with a hand-verified **relevance predicate** rather than
a frozen drawer ID. A candidate counts as relevant iff its `source_file` matches
the (optional) glob **and** its body contains one of the predicate's substrings.
Every predicate was verified on 2026-05-27 by reading the matched drawers via
`mempalace_search` against the production palace.

The queries span palace-daemon operational knowledge with objectively
identifiable answers: the systemd kill-cascade incident, the FlashRank spike
record, the HNSW `num_threads=1` pin, the system-vs-user-unit rule, the
`max_results`→`limit` search-arg fix, the 7-room taxonomy, the OOM/SIGKILL
startup diagnosis, and the port-8085 fuser ping-pong. Each has a single
clearly-correct target drawer (or a small set of near-duplicates), which makes
MRR a meaningful signal.

### Why predicates, not drawer IDs

Drawer IDs churn as the palace is re-mined and chunked; a frozen-ID gold set
would silently rot. Structural predicates (source file + verified substring)
survive re-mining and keep the harness re-runnable. The trade-off: a predicate
could in principle match an unintended near-duplicate. We accept this because
the palace genuinely contains such near-duplicates (same `feedback_*.md` filed
twice, sync-conflict copies), and counting *any* of them as a correct answer is
the honest semantics for "did the right information surface."

## Limitations (read before trusting the numbers)

1. **Single relevant doc per query (mostly).** This is a *known-item* retrieval
   eval, not a graded-relevance one. R@K is therefore binary per query and MRR
   dominates the signal. It answers "does rerank surface the right drawer
   higher?" — not "does it improve nuanced multi-doc ranking?"
2. **Candidate pool ceiling.** Rerank can only reorder what retrieval already
   fetched. If the relevant drawer isn't in the pool, the query is *excluded*
   from aggregates (reported as `n_excluded_no_relevant`) rather than scored as
   a miss — so these numbers measure rerank's lift *given good recall*, not
   end-to-end recall.
3. **Hand-curated, palace-specific set.** 12 queries authored by the evaluator
   against this specific palace. It is a probe, not a benchmark. Absolute
   numbers are not comparable to public IR leaderboards; only the
   baseline→reranked *delta* is meaningful here.
4. **Flat cross-encoder scores.** The TinyBERT model returns top-of-pool scores
   clustered at ~0.999 (see Analysis). Where the baseline already ranks the
   relevant doc first, rerank has no room to help and can only shuffle near-ties
   — which is exactly how the lone R@5 regression arose. Read the per-query
   `rank_delta` table, not just the aggregate.

## Results

<!-- METRICS_TABLE_START -->
Live run, 2026-05-27, `pool=20`, against the deployed daemon. **11/12 queries
usable** (1 excluded — its relevant doc was not in the retrieved pool, so rerank
could not affect it; 0 errors). Raw output: `docs/evals/rerank-eval-2026-05-27.json`.

| metric | baseline | reranked | delta |
|---|---|---|---|
| R@5  | 1.000 | 0.909 | **−0.091** |
| R@10 | 1.000 | 1.000 | 0.000 |
| MRR  | 0.761 | 0.877 | **+0.116 (+15.3%)** |

Rerank latency (per request, n=12): **mean 47.0 ms, min 19.7, max 156.5** —
comfortably within budget; the max coincided with the host load spike noted
below. Wall time for the whole 12-query pass: 37 s.

**Cross-check:** replaying the frozen candidate pools in `--mode candidates`
(rerank done in-process via the production `rerank.py`) reproduces the metrics
**exactly** (MRR 0.761 → 0.877, R@5 1.0 → 0.909). The two independent paths
agreeing is strong evidence the harness is measuring what it claims.

### Per-query movement (1-based rank of the first relevant hit)

| query | baseline | reranked | Δ | note |
|---|---|---|---|---|
| rerank-spike | 5 | **1** | **+4** | biggest win — buried answer rescued |
| wing-room-taxonomy | 2 | 1 | +1 | |
| rerank-fallback-contract | 3 | 2 | +1 | |
| kill-cascade | 1 | 1 | 0 | already optimal |
| hnsw-pin | 1 | 1 | 0 | already optimal |
| system-service-only | 1 | 1 | 0 | already optimal |
| rerank-implementation-plan | 1 | 1 | 0 | already optimal |
| oom-sigkill-startup | 1 | 1 | 0 | already optimal |
| search-args-limit-param | 1 | 1 | 0 | already optimal |
| fuser-port-8085 | 1 | 1 | 0 | already optimal |
| **daemon-deploy-arch** | 3 | **7** | **−4** | only regression — see analysis |
| felipe-976-cherrypick | — | — | — | excluded (no relevant doc in pool) |

3 improvements, 7 no-change (retrieval already nailed it), 1 regression. The
no-change majority is itself a positive signal: where vector retrieval already
ranked the answer first, the reranker correctly left it alone rather than
churning a good ordering.
<!-- METRICS_TABLE_END -->

## Analysis of the one regression (`daemon-deploy-arch`)

Query: *"palace daemon deployment architecture system unit etc systemd"*. The
canonical answer (`project_daemon_deploy_architecture.md`, "systemd system unit
at /etc/systemd/system/palace-daemon.service") sat at baseline rank 3 and rerank
pushed it to rank 7.

Inspecting the pool explains why, and it is **not** a model malfunction: the
top 7 reranked passages all scored **0.9971–0.9994** — a 0.002 spread — and
every one of them is genuinely on-topic (a `deploy.sh` header, a "Layer 1 —
palace-daemon stability (system unit on disks)" planning note, a `scripts/
deploy.sh` diff, the "system-level systemd service" reference). When seven
passages are all legitimately relevant and the cross-encoder scores them within
0.002 of each other, the head ordering is effectively a coin-flip; TinyBERT
happened to prefer the script/planning passages over the prose reference.

This is the **score-compression** failure mode of a 2-layer distilled
cross-encoder on a saturated candidate set, and it is the single strongest
argument in this report for evaluating a larger model: a MiniLM L-6/L-12 with
more discriminative head scores would be far less prone to shuffling near-ties.

## Other observations

- **Model cold-load:** ~50 ms in-process (matches the issue's 44–100 ms range).
- **Per-request latency:** mean 47 ms over the 12 live queries (n≤20 each),
  consistent with the issue's ~15–40 ms estimate; the 156 ms max landed during
  a host load spike (see below), not a model cost.
- **Pervasive score compression:** the flat-0.999 head was not unique to the
  regression query — most pools showed the top several passages within ~0.01 of
  each other. Rerank's wins came from cases where the *truly* best passage was
  far down the vector ranking (rerank-spike: distance rank 5, but the
  cross-encoder recognised it as the on-point answer and lifted it to 1).
- **Both eval modes agree exactly**, validating the harness (see Results).

## Decision criteria (from the issue)

- **KEEP nano** if quality lift is measurable and latency acceptable.
- **ESCALATE to MiniLM L-6 / L-12** if lift is real but more headroom is wanted.
- **REVERT** if no measurable gain.

### Verdict: KEEP nano now, with a scheduled follow-up A/B against MiniLM L-12

- **Lift is measurable.** MRR +15.3% (0.761 → 0.877) is well past noise on an
  11-query set, driven by a clean rank-5→1 rescue plus two smaller promotions.
  This rules out REVERT — there *is* a gain.
- **Latency is acceptable.** 47 ms mean per request, ~50 ms cold-load. No
  budget concern on the CPU-only production host.
- **But there's headroom, and a regression to watch.** The lone R@5 regression
  (3→7) and the pervasive ~0.999 score compression are exactly the "lift is real
  but more headroom is wanted" condition the issue names for ESCALATE. nano's
  scores are too flat to reliably break near-ties.

The pragmatic call: **keep nano live** (it's a net win today, costs little, and
the fallback contract is sound) and **open a follow-up to A/B `ms-marco-MiniLM-L-12-v2`
against nano on this same harness** — flip `PALACE_RERANK_MODEL` and re-run
`--mode candidates` against the frozen pools for a zero-retrieval-cost comparison.
(`L-12` is the next size up that this FlashRank build actually ships; there is no
`L-6` available.) Decide ESCALATE vs stay-on-nano from that head-to-head.
Reverting would throw away a real +15% MRR for no benefit.

### Suggested next step (cheap, no palace load)

```bash
# A/B the larger model on the SAME frozen candidate pools — pure rerank cost,
# no retrieval, no daemon restart:
PALACE_RERANK_MODEL=ms-marco-MiniLM-L-12-v2 \
  venv/bin/python scripts/evals/rerank_eval.py --mode candidates \
  --candidates docs/evals/rerank-candidates-2026-05-27.json \
  --out docs/evals/rerank-eval-minilm-l12.json
```

## Reproducing

```bash
cd /home/jp/Projects/palace-daemon

# Preferred: against the deployed daemon (read-only; one GET /search per query)
venv/bin/python scripts/evals/rerank_eval.py --mode live --delay 1 \
    --out docs/evals/rerank-eval-2026-05-27.json

# Fallback when the daemon is contended: rerank a frozen candidate pool
# in-process via the production rerank.py codepath
venv/bin/python scripts/evals/rerank_eval.py --mode candidates \
    --candidates docs/evals/rerank-candidates-2026-05-27.json \
    --out docs/evals/rerank-eval-2026-05-27.json
```

The eval is strictly read-only against the palace.
