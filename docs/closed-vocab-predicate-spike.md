# Closed-Vocabulary Predicate Mapping — Design Spike (issue #72)

Read-only design spike. **No production graph mutation was performed.**

## Question

#50 → PR #61 / #71 proved the AGE knowledge graph carries **64,029** distinct
`r.relation_type` predicate strings over **1,724,791** RELATION triples, and
that the conservative surface-form normalizer ([`kg_predicate_norm.py`]) trims
only ~3% of the *distinct* vocabulary — a long tail of one-off verbose LLM
paraphrases dominates the cardinality.

This spike answers: **can a small curated canonical ontology + embedding
nearest-canonical mapping collapse the 64k vocabulary to "dozens" of relations
while still covering the bulk of triples?** The headline signal is
**triple coverage** (frequency-weighted), because the long tail dominates the
*distinct* count but not the *triple* count.

## Method

1. **Live vocabulary + frequencies** (READ-ONLY `/cypher` GROUP BY over
   `r.relation_type` — no mutation):

   ```bash
   set -a; source ~/.config/palace-daemon/env; set +a
   curl -sS -H "X-Api-Key: $PALACE_API_KEY" -H "Content-Type: application/json" \
        "$PALACE_DAEMON_URL/cypher" \
        -d '{"cypher":"MATCH ()-[r:RELATION]->() RETURN r.relation_type AS rt, count(*) AS n"}' \
     | jq '[.rows[] | select(.rt != null) | {rt:(.rt|tostring), n:.n}]' > freq.json
   ```

2. **Canonical set** ([`kg_canonical_vocab.CANONICAL_RELATIONS`]): a curated
   **39-relation** ontology. Seed selection: the highest-frequency predicates
   *after* `normalize_predicate` (`is_a`, `contains`, `depends_on`,
   `created_by`, `uses`, `imports`, …) which alone cover the majority of
   triples, rounded out with schema.org / SKOS-style relations (`references`,
   `part_of`, `has_property`, `derived_from`, `related_to`, …) so common
   paraphrase clusters have a home. Each canonical carries a short **gloss**;
   the gloss (not the bare token) is embedded so nearest-neighbour matching has
   more semantic surface.

3. **Mapping**: surface-normalize each predicate, embed it with mempalace's own
   ONNX MiniLM (384-dim — the *same model the corpus embeds with*, so no new
   heavy dependency and similarity is measured in the corpus's space), and take
   the cosine-nearest canonical gloss. Below the distance threshold → `other`
   (explicit long-tail bucket). Code tokens drop via `normalize_predicate`.
   If the embedding model can't load, a lexical (Jaccard token-overlap)
   fallback runs and the report flags the downgrade.

## Result (threshold 0.45, embedding mode)

Corpus: **1,060,950 entities; 1,724,791 RELATION triples; 64,029 distinct predicates.**

| Metric | Value |
| --- | --- |
| Canonical relations defined | **39** |
| Mapped cardinality (canonicals used) | **39** (+ `other` bucket) |
| **Triple coverage by canonical** | **65.1%** (1,122,874 triples) |
| → `other` (long tail) | 34.4% (592,815 triples; **57,505** distinct raws) |
| → dropped (code tokens) | 0.5% (9,102 triples) |
| Distinct-cardinality reduction | 64,029 → 39 (**~1,640×**) |

### Top canonical collapses (by triple weight)

| Canonical | Triples | Raw forms | Examples |
| --- | --- | --- | --- |
| `is_a` | 376,203 | 50 | is, are, was, type, were |
| `contains` | 316,517 | 284 | has, includes, have, include, contain |
| `depends_on` | 69,501 | 101 | requires, needs, depend_on, relies_on |
| `creates` | 52,596 | 286 | created, create, produces, generates |
| `created_by` | 40,385 | 284 | was_created, is_created_by, made, wrote |
| `uses` | 33,014 | 506 | is_used_for, used_for, use, used |
| `imports` | 26,838 | 244 | import, is_imported, imported, imported_from |
| `located_at` | 19,112 | 232 | is_located_in, located_in, located_at |
| `returns` | 17,272 | 118 | return, value, output, evaluates |
| `provides` | 16,267 | 99 | exposes, provide, expose, is_offered |

The collapses are semantically coherent: tense/voice variants (`is`/`are`/`was`),
synonyms (`requires`/`needs`/`relies_on`), and verbose paraphrases
(`is_used_for`/`used_for`/`is_used`) all fold to one relation.

## Threshold sensitivity

Threshold 0.45 (cosine) is the reported operating point — it keeps the
collapses semantically tight while covering ~⅔ of triples. Lowering it pulls
more of the long tail onto a canonical (higher coverage) at the cost of
precision (more semantically-loose bins); raising it sends more to `other`.
The `lexical` fallback (no embedding model) lands at **56.5%** coverage on the
same set — embeddings buy ~9 points, confirming the mechanism choice.

To sweep cheaply, embed once into a cache and re-run at different thresholds
(the cache makes each extra threshold instant):

```bash
export PALACE_PRED_EMBED_CACHE=/tmp/pred_embed_cache.npz
for t in 0.40 0.45 0.50 0.55; do
  venv/bin/python scripts/canonical_vocab_report.py --freq-file freq.json --threshold $t \
    | grep "TRIPLE COVERAGE"
done
```

(CPU embedding of the ~62k unique predicates is a one-time ~20 min cost; every
threshold after the first reuses the cached vectors.)

## Interpretation

- **Viability: yes.** 39 canonicals cover ~two-thirds of all triples; distinct
  cardinality drops ~1,640×. The `other` bucket is 34% of triples but **89% of
  the distinct raws** (57,505 / 64,029) — confirming the long tail is real but
  triple-light. Most of `other` is genuinely idiosyncratic one-off relations
  the LLM invented; lowering the threshold pulls more in at the cost of
  precision (see sweep).
- **Embedding nearest-canonical is the right mechanism**, not a hand-written
  synonym map: a 64k→dozens map cannot be enumerated by hand, but the same
  MiniLM the corpus already uses places paraphrases near their canonical gloss
  cheaply.

## Recommendation (JP decision)

Two ways to get a clean closed vocabulary:

**(a) Post-extraction mapping pass in the write path** — run
`CanonicalMapper.map_predicate` on each emitted predicate before the triple is
written to AGE. Pros: no extractor/LLM change; deterministic; re-runnable over
the existing corpus as a one-shot migration; the `other` bucket preserves
recall for genuinely novel relations. Cons: adds an embedding call per write
(cheap — MiniLM, already loaded); a wrong-bin mapping is a silent semantic
error (mitigated by the threshold + `other` fallback).

**(b) Constrain mempalace's extractor to a closed enum** — give the LLM the
39-relation list and a "skip if no fit" instruction at extraction time. Pros:
highest quality; the model picks intent, not surface-form nearest-neighbour;
no `other` bucket noise. Cons: requires re-running extraction over the corpus
(expensive); doesn't fix the 1.06M entities already extracted; ties the schema
to a prompt that's harder to version than a Python module.

**Recommended: (a) first, then (b).** The 65% coverage shows a post-hoc mapping
pass cleans the existing graph immediately and cheaply (one-shot migration over
`r.relation_type`, fully reversible since the raw string can be retained as a
property). Use the spike's coverage numbers to tune the canonical set, *then*
fold the finalized ontology into the extractor prompt (b) so new writes are
clean at the source. Doing (b) alone strands the existing 1.06M-entity corpus;
doing (a) alone leaves new writes noisy. The combination is additive.

## Reproduce

```bash
# pure tests (no model load)
venv/bin/python -m pytest tests/test_kg_canonical_vocab.py -q

# dry-run report (embedding mode; caches vectors so threshold sweeps are fast)
export PALACE_PRED_EMBED_CACHE=/tmp/pred_embed_cache.npz
venv/bin/python scripts/canonical_vocab_report.py --freq-file freq.json --threshold 0.45
# lexical fallback (no model):
venv/bin/python scripts/canonical_vocab_report.py --freq-file freq.json --lexical
```

[`kg_predicate_norm.py`]: ../kg_predicate_norm.py
[`kg_canonical_vocab.CANONICAL_RELATIONS`]: ../kg_canonical_vocab.py
