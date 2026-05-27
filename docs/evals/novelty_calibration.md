# NCD novelty_score calibration (#47)

Calibrating the gzip-NCD `novelty_score` distribution (`novelty.py`, landed in
#45, wired into `POST /memory` at `main.py:1835-1852`) against the **existing**
palace corpus, since waiting days for fresh writes wasn't an option.

- **Script:** `scripts/calibrate_novelty.py` (read-only; uses the daemon MCP
  read tools `mempalace_list_drawers` + `mempalace_get_drawer`).
- **Scoring:** imports `novelty.score_novelty` directly — the *same* function
  the daemon runs at write time. The NCD formula is never reimplemented.
- **Corpus:** ~375k drawers, postgres backend, daemon on `familiar:8085`.
- **Raw results:** `docs/evals/novelty_calibration.json` (regenerate with the
  command below).

## Methodology — block rolling-window

Production scores a new write as the **minimum NCD** against the `N` most
recent drawers in the same `(wing, room)` (`PALACE_NOVELTY_WINDOW`, default 20).
To reproduce this on historical data without waiting for new writes, the script
uses a *block* method:

1. Discover `(wing, room)` groups by walking `mempalace_list_drawers` from
   random offsets across the corpus.
2. For each group, pull a contiguous block of `window + per_group` recent
   drawers in list order (which approximates insertion order).
3. For each position `i >= window` in the block, treat `drawer[i]` as the
   "new write" and `drawers[i-window:i]` as its rolling window — i.e. exactly
   "the N drawers that existed immediately before this one." Score with
   `novelty.score_novelty`.

This amortizes to ~1 full-content fetch per scored sample instead of
`window + 1`, which matters over SSH against a 375k-drawer corpus.

### Reproduce — live (against `familiar:8085`)

```bash
# On a host where the daemon is reachable (familiar: localhost):
set -a; . ~/.config/palace-daemon/env; set +a
python3 scripts/calibrate_novelty.py \
    --groups 60 --per-group 8 --window 20 \
    --out docs/evals/novelty_calibration.json
```

### Reproduce — offline validation (no daemon)

When the daemon is unreachable, `--offline-sample` runs the *identical*
scoring / stats / histogram pipeline against a built-in synthetic corpus
(`build_synthetic_corpus`) seeded with three redundancy archetypes —
near-duplicate auto-save checkpoints, iterative edits, and independent varied
prose — across 7 wings x 7 rooms. This is how the harness was validated
end-to-end while `familiar` was down (see results below):

```bash
python3 scripts/calibrate_novelty.py --offline-sample \
    --window 20 --per-group 12 \
    --out docs/evals/novelty_calibration_offline.json
```

## CRITICAL FINDING — the live scorer is currently a no-op

`compute_novelty_for_write` (`novelty.py:166-172`) reads each window member's
text from the drawer fields **`text` / `content` / `preview`**:

```python
text = d.get("text") or d.get("content") or d.get("preview") or ""
```

But `mempalace_list_drawers` (the tool it calls to build the window) returns the
field **`content_preview`** — not any of those three. Verified against the live
daemon:

```json
{"drawers":[{"drawer_id":"...","wing":"storyvox","room":"sessions",
             "tags":[],"content_preview":"AUTO-SAVE:..."}], "total":375302}
```

Consequence: the window `texts` list is **always empty**, so every live write
hits the `not existing_texts` branch in `score_novelty` and returns
`status="no_window"`, `novelty_score=1.0`. **No write has ever been scored
against real neighbours since #45 landed.** The feature is dark.

**Fix (one line, the #1 follow-up):** add `content_preview` to the fallback
chain in `compute_novelty_for_write`:

```python
text = d.get("text") or d.get("content") or d.get("preview") \
    or d.get("content_preview") or ""
```

### Secondary fidelity issue — previews are truncated

`content_preview` is truncated to ~200 chars. Even after the field-name fix,
the window would be scored against *truncated* neighbours, not full content.
NCD on 200-char prefixes is noisier and biased high (less shared substring to
find), inflating novelty for long drawers that happen to share a boilerplate
header (e.g. all the `AUTO-SAVE:...` diary checkpoints share a prefix but
diverge in the body). For a faithful score the write path should compare
against **full** neighbour content (an extra `get_drawer` per window member, or
have `list_drawers` return full content for small windows).

The calibration script computes the **full-content** distribution by default
(what a corrected scorer *should* see). `--use-preview` reproduces the
truncated distribution for comparison.

## Empirical distribution

> **The numbers below are from the OFFLINE SYNTHETIC corpus** (seed 47,
> window 20, per_group 12, 588 scored samples) — they validate the harness and
> the *shape* of the analysis, not the production magnitudes. The live run
> against `familiar:8085` is the remaining checkbox (see "Status"). Synthetic
> NCD values are compressed relative to real prose because gzip behaves
> differently on short, vocabulary-limited strings; the **bimodality and the
> methodology** are what's demonstrated, not the absolute cutoffs.

**Overall (offline-synthetic, `status=ok`, window=20, n=588):**

| stat | value |
|------|-------|
| min | 0.139 |
| p05 | 0.156 |
| p10 | 0.160 |
| p25 | 0.169 |
| median | 0.182 |
| mean | 0.354 |
| p75 | 0.548 |
| p90 | 0.577 |
| p95 | 0.588 |
| max | 0.610 |
| stdev | 0.191 |

**Histogram (20 bins, 0=duplicate .. 1=novel):**

```
[0.10-0.15)    14 |##
[0.15-0.20)   286 |##################################################
[0.20-0.25)     0 |
... (empty 0.20-0.45) ...
[0.45-0.50)    20 |###
[0.50-0.55)   122 |#####################
[0.55-0.60)   137 |########################
[0.60-0.65)     9 |##
```

**Bimodal vs continuous: clearly BIMODAL.** A tight low cluster at ~0.14-0.20
(the near-duplicate checkpoint + iterative-edit groups) and a higher cluster at
~0.45-0.62 (varied prose), separated by an empty valley across 0.20-0.45. This
is exactly the structure #47 asks about, and it confirms a percentile/valley
threshold approach is appropriate. The real palace is *expected* to show the
same qualitative split, given how many drawers are `AUTO-SAVE:...` checkpoints
sharing a fixed prefix — but the valley location will differ.

## Proposed thresholds

Because the distribution is bimodal with a clear valley, thresholds are best
pinned to the **valley** (the empty band between modes), with percentiles as a
fallback if the live distribution turns out more continuous than the synthetic
one. Expressed as tunable env vars so they don't require a redeploy (mirroring
`PALACE_NOVELTY_WINDOW`):

| label | rule | derivation | synthetic value |
|-------|------|-----------|-----------------|
| **redundant** | `score <= PALACE_NOVELTY_REDUNDANT_HI` | top of the low cluster / bottom of the valley | ~0.20 (here) |
| **borderline** | between the two | review-on-demand band | 0.20-0.45 (here) |
| **novel** | `score >= PALACE_NOVELTY_NOVEL_LO` | bottom of the high cluster | ~0.45 (here) |

Concretely, set `REDUNDANT_HI` at the right edge of the low mode and `NOVEL_LO`
at the left edge of the high mode (midpoint of the valley if you want a single
cut). **Re-derive both from the live histogram** — the synthetic 0.20 / 0.45
are placeholders proving the method, not production cutoffs.

## Follow-ups (NOT built in this PR)

1. **[bug] Field-name fix** (above) — `content_preview` in the fallback chain.
   Highest priority: the feature does nothing until this lands. Small, belongs
   in its own PR/commit with a regression test.
2. **[bug] Full-content windows** — compare against full neighbour content, not
   truncated previews.
3. **Curation endpoint** — `GET /curation/low-novelty?wing=&room=&max_score=`
   surfacing drawers below `REDUNDANT_HI` for review/dedup. Proposed shape:
   paginated list of `{drawer_id, wing, room, novelty_score, most_similar_id}`.
   Requires persisting `novelty_score` to drawer metadata at write time (it is
   currently returned to the client but not stored).
4. **Retrieval-time de-weighting** — expose stored `novelty_score` in
   `/search` results so retrieval can down-weight near-duplicates (multiply
   `effective_distance` by a mild factor of `novelty_score`, or drop members of
   a near-duplicate cluster beyond the first). Needs (3)'s persistence first.

## Status

- [x] Calibration harness (`scripts/calibrate_novelty.py`) — read-only live
      path + `--offline-sample` synthetic path, both through the *same*
      `novelty.score_novelty`. Unit-tested (`tests/test_calibrate_novelty.py`).
- [x] Pipeline validated end-to-end on the synthetic corpus; distribution is
      bimodal and the percentile/valley threshold method works.
- [x] Latent no-op bug identified + one-line fix proposed (the highest-impact
      outcome of this issue — the feature is currently dark).
- [x] Methodology + threshold framework documented.
- [ ] **Live distribution run against `familiar:8085`** — deferred: `familiar`
      sshd was unresponsive (banner-exchange timeout) and the daemon's port
      8085 is firewalled from other hosts, so the live corpus is unavailable.
      Re-run the live command above once `familiar` is reachable to fill the
      production table/histogram and pin `REDUNDANT_HI` / `NOVEL_LO` from the
      real valley.
