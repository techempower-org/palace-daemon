# Deployed-retrieval determinism across restarts

**Date:** 2026-05-30
**Author:** Echo (SME dream-team)
**Branch / PR:** `probe/retrieval-determinism`
**Prompted by:** SME `techempower-org/multipass-structural-memory-eval#117` — re-querying the same `lme_*` wings at `/search` before vs after the 2026-05-30 daemon restart reordered **49% of top-5 results on identical-looking data** (and grew per-hit context +32%). A deterministic reranker reloading the same model shouldn't reorder. This probe asks: **is deployed `/search` retrieval deterministic across a daemon restart, and what caused the #117 reorder?**

## Bottom line

**The rerank stage is deterministic across restart — proven.** The #117 reorder was **not** reranker nondeterminism; it was a **candidate-set change** upstream of the reranker, caused by a one-time **2026-05-29 DB rebackfill** that re-merged checkpoint drawers into the searchable collection. This is an expected one-time data event, **not an ongoing regression**. One latent risk worth hardening is flagged below (unpinned reranker package version).

## What `/search` actually does

`search_routes.py::search` (the plain `GET /search` both #117 runs used) is a three-stage pipeline:

1. **Candidate retrieval** — `mempalace_search` (vector ANN over `mempalace_drawers`), fetching exactly `limit` candidates. `main._search_args` passes `limit` straight through, so the rerank pool size == the requested `limit` (5 / 20 / 50); there is no fixed over-fetch.
2. **Kind filter** — `_apply_kind_filter` drops Stop-hook checkpoint drawers when `kind=content` (re-added in #199). Both #117 runs used `kind=all`, so this stage was a no-op for them.
3. **Rerank** — `rerank.rerank_response` → FlashRank cross-encoder (`ms-marco-TinyBERT-L-2-v2`), reordering the candidates by relevance to the query.

Only stages 1 and 3 can reorder top-5. Stage 3 is the suspect the #117 trace named; this probe clears it.

## The rerank stage is deterministic (code + empirical)

**Code analysis** (`rerank.py`, `flashrank/Ranker.py`):
- FlashRank's pairwise ONNX path is a pure function of `(query, passage_text, model_weights)`: `session.run(...)` on CPU, sigmoid/softmax over logits — no sampling, no dropout, no RNG at inference.
- Final ordering is `passages.sort(key=lambda x: x["score"], reverse=True)` — Python's **stable Timsort**, so score ties preserve input order deterministically.
- `rerank_hits` reconstructs the output by original index with a `seen` set and appends unrankable graph-stubs at the tail in original order — no dict-iteration-order dependence, no float-tie reorder.

**Empirical** (`scripts/evals/rerank_determinism_probe.py`, and pinned as `tests/test_rerank.py::TestRerankDeterminism`):
- In-process, the same `(query, 20 candidates)` reranked 5× → **byte-identical order + scores**.
- Two **fresh processes**, each loading the ONNX model from scratch (a faithful restart simulation), reranking the same fixed input → **identical top-5 order AND identical scores to <1e-9**.

```
[1] IN-PROCESS REPEAT (5×):           DETERMINISTIC   top-5 = [0, 14, 5, 12, 4]
[2] FRESH-PROCESS RELOAD (×2):        order identical: True   scores identical (<1e-9): True
VERDICT: rerank stage is DETERMINISTIC across restart
```

So a restart that reloads the **same** model and receives the **same** candidates produces the **same** top-5. The reranker is exonerated.

## What actually changed at the restart — candidate set, via the DB rebackfill

The deployed daemon process restarted **2026-05-30 00:16 PDT (07:16 UTC)** — it straddles the two #117 measurements:

| event | UTC |
|---|---|
| #117 cached limit=5 capture | 2026-05-29 17:37 |
| #199 commit "re-add kind filter" (documents the rebackfill) | 2026-05-30 03:46 |
| #202 commit (age-fused hydration) | 2026-05-30 07:15 |
| **daemon restart** | **2026-05-30 07:16** |
| #117 live limit=5 re-query | 2026-05-30 15:57 |

The restart picked up #199 + #202, **but neither is the cause** of the plain-`/search` reorder:
- **#202** touches only the `/search/age-fused` graph-only hydration handler — never reached by plain `/search`.
- **#199** re-added the `kind=content` filter — but both #117 runs used `kind=all`, so the filter was inert for them.

The real driver is the data event #199's commit message **documents**: *"The 2026-05-29 DB rebackfill silently re-merged checkpoint drawers into `mempalace_drawers`."* Confirmed on the live palace — **862 checkpoint-like drawers** now sit in the searchable collection (out of 409k). Adding ~862 vectors to the ANN collection changes the nearest-neighbour set returned for a fixed query at a fixed `limit`, which feeds the (deterministic) reranker a **different candidate pool** → a different reordered top-5. That is exactly the #117 signature: same wings, different top-5 selection, fuller chunks surfacing.

**Ruled out as causes** (each checked): rerank nondeterminism (proven deterministic above), the #202 age-fused fix (wrong code path), the `kind` filter (`kind=all` used), and any change to the `lme_*` haystack itself (those drawers were filed 2026-05-25 and have fully-populated `document`/`doc_tsv`).

## Is deployed retrieval deterministic across restarts? — Yes, conditionally

For a **fixed collection + fixed reranker model**, deployed `/search` is deterministic across restarts: candidate ANN retrieval over an unchanged index is reproducible, and the reranker is proven reproducible. The #117 reorder was a **one-time data migration** (the rebackfill), not a per-restart coin-flip. A restart with no intervening data change will reproduce the same top-5.

### Latent risk worth hardening (recommendation, not a fix in this PR)

`requirements.txt` pins **`flashrank>=0.2.10`** (a floor, not an exact version) and `PALACE_RERANK_MODEL` is unset (defaults to `ms-marco-TinyBERT-L-2-v2`). The model *name* is stable, but a `pip install` on a fresh deploy could pull a newer flashrank whose bundled ONNX weights or tokenizer differ — which **would** reorder top-5 across that deploy, this time for real. Two cheap guards:
1. **Pin flashrank to an exact version** (e.g. `flashrank==0.2.10`) and pin `PALACE_RERANK_MODEL` explicitly in the systemd `Environment=`, so the reranker is byte-stable across deploys.
2. (Optional) add a startup log line recording the resolved flashrank version + model name + ONNX file hash, so a future cross-restart reorder can be attributed to a model change in one glance.

These are defensive; the current deployed behavior is determinate given the present pins.

## Artifacts

- Determinism tests (pinned, run when flashrank present): `tests/test_rerank.py::TestRerankDeterminism`
- Standalone probe (in-process repeat + fresh-process reload): `scripts/evals/rerank_determinism_probe.py`
- Code paths analyzed: `search_routes.py::search`, `rerank.py`, `flashrank/Ranker.py::rerank`
- Originating finding: SME `docs/benchmarks/2026-05-30-deployed-e2e-ladder.md`
