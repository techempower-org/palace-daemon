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
`window + 1`, which matters against a 375k-drawer corpus.

### Reproduce — live (against `familiar:8085`)

```bash
# PALACE_DAEMON_URL + PALACE_API_KEY come from the env file:
set -a; . ~/.config/palace-daemon/env; set +a
venv/bin/python scripts/calibrate_novelty.py \
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

## Key finding — the live scorer was a no-op (FIXED in #63)

`compute_novelty_for_write` originally read each window member's text from the
drawer fields `text` / `content` / `preview`, but `mempalace_list_drawers`
returns the body under **`content_preview`** — none of those three. The window
`texts` list was therefore **always empty**, so every write hit the
`not existing_texts` branch and returned `status="no_window"`,
`novelty_score=1.0`. **No write was scored against real neighbours from #45
until the fix.** The feature was silently dark.

**Fixed in #63** by adding `content_preview` to the fallback chain (and, in this
PR, guarding against malformed non-dict drawer entries so a bad row can't throw
the loop into the outer `except` and silently re-create the no-op). The live
distribution above was produced *after* that fix, so it reflects real scoring.

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

## Empirical distribution (LIVE — `familiar:8085`, full-content, window=20)

Live run: 35 `(wing, room)` groups, 280 scored drawers, 980 full-content
fetches, seed 47. Raw: `docs/evals/novelty_calibration.json`.

**Overall (`status=ok`, n=280):**

| stat | value |
|------|-------|
| min | 0.028 |
| p05 | 0.126 |
| p10 | 0.165 |
| p25 | 0.402 |
| median | 0.622 |
| mean | 0.558 |
| p75 | 0.725 |
| p90 | 0.785 |
| p95 | 0.821 |
| max | 0.952 |
| stdev | 0.222 |

**Histogram (20 bins, 0=duplicate .. 1=novel):**

```
[0.00-0.05)     6 |########
[0.05-0.10)     4 |#####
[0.10-0.15)    13 |##################
[0.15-0.20)    10 |##############
[0.20-0.25)     4 |#####
[0.25-0.30)    13 |##################
[0.30-0.35)    11 |###############
[0.35-0.40)     9 |############
[0.40-0.45)     5 |#######
[0.45-0.50)     8 |###########
[0.50-0.55)    10 |##############
[0.55-0.60)    34 |##############################################
[0.60-0.65)    29 |#######################################
[0.65-0.70)    37 |##################################################
[0.70-0.75)    37 |##################################################
[0.75-0.80)    28 |######################################
[0.80-0.85)    17 |#######################
[0.85-0.90)     4 |#####
[0.90-0.95)     0 |
[0.95-1.00)     1 |#
```

**Bimodal vs continuous: CONTINUOUS and right-skewed — NOT bimodal.** The mass
sits in a broad "novel" hump across **0.55-0.85** (peak around 0.65-0.75), with
a long **redundant tail below ~0.20** (p05=0.126, p10=0.165, min 0.028) and the
0.20-0.55 band populated *throughout* — there is no empty valley separating two
modes. (The earlier synthetic corpus looked bimodal because its archetypes were
artificially separated; real palace content varies continuously.)

Practically: most drawers are genuinely novel relative to their 20 most-recent
wing/room siblings, a minority are near-duplicates (the redundant tail), and
there's a smooth middle. A *single* global cut would mislabel content in rooms
whose baseline novelty differs — see below.

### Per-room — baselines differ substantially

| room | n | p10 | p25 | median | p75 | p90 | mean |
|------|---|-----|-----|--------|-----|-----|------|
| references | 144 | 0.173 | 0.469 | 0.636 | 0.731 | 0.781 | 0.570 |
| architecture | 40 | 0.363 | 0.561 | 0.649 | 0.718 | 0.776 | 0.610 |
| discoveries | 40 | 0.126 | 0.147 | **0.304** | 0.587 | 0.705 | 0.387 |
| planning | 32 | 0.496 | 0.589 | **0.677** | 0.755 | 0.821 | 0.663 |
| problems | 24 | 0.215 | 0.382 | 0.637 | 0.718 | 0.791 | 0.548 |

`discoveries` is far more redundant (median 0.30) than `planning`/`architecture`
(median ~0.65-0.68) — a ~0.37 spread in medians across rooms. Per-wing variation
is just as wide (e.g. `palace_daemon` / `projects` wings median ~0.30-0.37 vs
`storyvox` median 0.73). A global threshold is the wrong tool here.

## Proposed thresholds — per-room percentiles

Because the distribution is continuous and per-room baselines diverge,
thresholds should be **per-room percentiles of that room's own distribution**,
not a single global NCD cut:

- **redundant**: `score <= p15(room)` — the room's own low tail. Concretely,
  ~0.13 for `discoveries`, ~0.40 for `architecture`/`planning`. A drawer in the
  bottom ~15% of its room's novelty is a near-duplicate *for that room*.
- **borderline**: `p15(room) < score < p60(room)` — review-on-demand.
- **novel**: `score >= p60(room)` — the room's upper mass.

Implementation: store per-room percentile cut-points (recomputed periodically
from a calibration run like this one) rather than hard-coding NCD values. As a
simpler v1, a single global pair derived from the overall distribution works as
a coarse default — **redundant `<= ~0.20`** (overall p10-p15), **novel
`>= ~0.55`** (where the hump begins) — but it will over-flag `planning`/
`architecture` as redundant and under-flag `discoveries`. Expose the cut-points
as tunable config (e.g. `PALACE_NOVELTY_REDUNDANT_PCT` / `_NOVEL_PCT`, or a
per-room JSON map) so they don't require a redeploy, mirroring
`PALACE_NOVELTY_WINDOW`.

### Offline synthetic baseline (harness validation only)

`docs/evals/novelty_calibration_offline.json` holds the original synthetic run
(`--offline-sample`, n=588). It was used to validate the harness end-to-end
while `familiar` was unreachable; its NCD magnitudes are compressed (gzip on
short vocabulary-limited strings) and its archetypes are artificially separated,
so it reads as bimodal. **The live numbers above supersede it** for any
threshold decision — keep the offline file only as a pipeline fixture.

## Follow-ups (NOT built in this PR)

1. **[bug] Full-content windows** — production scores against truncated
   `content_preview` (~200 chars), not full neighbour content. NCD on short
   prefixes is noisier and biased high. Fetch full content per window member
   (extra `get_drawer`), or have `list_drawers` return full content for small
   windows.
2. **Persist `novelty_score`** — the score is returned to the client at write
   time but not stored on the drawer. Curation and retrieval boosting both need
   it persisted to drawer metadata first.
3. **Curation endpoint** — `GET /curation/low-novelty?wing=&room=&max_pct=`
   surfacing drawers in the bottom percentile band *of their room* for
   review/dedup. Proposed shape: paginated list of
   `{drawer_id, wing, room, novelty_score, most_similar_id}`. Needs (2).
4. **Retrieval-time de-weighting** — expose stored `novelty_score` in `/search`
   results so retrieval can down-weight near-duplicates (multiply
   `effective_distance` by a mild factor of `novelty_score`, or drop members of
   a near-duplicate cluster beyond the first). Needs (2).
5. **Per-room threshold map** — wire the per-room percentile cut-points (above)
   into config and recompute periodically from a calibration run.

## Status

- [x] Calibration harness (`scripts/calibrate_novelty.py`) — read-only live
      path + `--offline-sample` synthetic path, both through the *same*
      `novelty.score_novelty`. Unit-tested (`tests/test_calibrate_novelty.py`).
- [x] No-op field-name bug fixed (#63) + hardened here against malformed rows.
- [x] **Live distribution captured** against `familiar:8085` (n=280) — the
      production numbers above. Distribution is **continuous/right-skewed, not
      bimodal**; per-room baselines vary widely.
- [x] Methodology + per-room threshold recommendation documented.
- [x] Closes #47.
