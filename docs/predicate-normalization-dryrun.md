# KG Predicate Normalization — Dry-Run Report (issue #50)

Build-and-report only. **No production graph mutation was performed.**

The AGE knowledge graph's `relation_type` vocabulary is unnormalized: the LLM
triple extractor emits ~1000+ distinct predicate strings (code tokens,
near-synonyms, negation fragments). This module adds a pure
`normalize_predicate(raw) -> str | None` pass and a dry-run report that shows
what *would* change if the pass were applied.

## How to reproduce

Pure-function tests (no DB):

```bash
venv/bin/python -m pytest tests/test_kg_predicate_norm.py -q
```

Dry-run report against the **bundled sample** (the contamination examples
enumerated in the issue — runs offline):

```bash
venv/bin/python scripts/predicate_norm_report.py
```

Dry-run report against the **live vocabulary** (READ-ONLY — `/cypher` marks its
transaction `READ ONLY`; the script never writes the graph):

```bash
set -a; source ~/.config/palace-daemon/env; set +a
venv/bin/python scripts/predicate_norm_report.py --live
# or, decoupled from the daemon being up at report time:
curl -sS -H "X-Api-Key: $PALACE_API_KEY" -H "Content-Type: application/json" \
     "$PALACE_DAEMON_URL/cypher" \
     -d '{"cypher":"MATCH ()-[r:RELATION]->() RETURN DISTINCT r.relation_type AS rt"}' \
  | jq '[.rows[].rt | select(. != null) | tostring]' > vocab.json
venv/bin/python scripts/predicate_norm_report.py --vocab-file vocab.json
```

## Live production cardinality (2026-05-27)

Captured against `familiar:8085` after the daemon came back up. **Note on the
probe path:** the issue's reproduction (`graph_stats.relationship_types | length`)
does *not* yield the predicate vocabulary on the current daemon —
`graph_stats` returns the *palace* graph (wings/rooms/tunnels) and `kg_stats`
returns only the two AGE edge **labels** (`["RELATION", "MENTIONS"]`), not the
`r.relation_type` property values. The actual predicate strings live inside the
`RELATION` edges, so the true vocabulary was enumerated with a **read-only**
Cypher query through `POST /cypher` (transaction marked `READ ONLY` — no
mutation):

```bash
set -a; source ~/.config/palace-daemon/env; set +a
curl -sS -H "X-Api-Key: $PALACE_API_KEY" -H "Content-Type: application/json" \
     "$PALACE_DAEMON_URL/cypher" \
     -d '{"cypher":"MATCH ()-[r:RELATION]->() RETURN DISTINCT r.relation_type AS rt"}' \
  | jq '[.rows[].rt | select(. != null) | tostring]' > live_vocab.json
venv/bin/python scripts/predicate_norm_report.py --vocab-file live_vocab.json
```

Corpus at probe time: **1,060,950 entities; 1,722,353 RELATION triples.**

`--live` runs the same `/cypher` query directly, but the `DISTINCT` walk over
1.7M edges is heavy — on a busy daemon it can time out or return HTTP 500
(both now exit cleanly with a hint rather than a traceback). For a graph this
large the `curl … > live_vocab.json` + `--vocab-file` path above is the
reliable capture; that is how the numbers below were produced.

| Metric | Bundled sample | **Live production** |
| --- | --- | --- |
| Original distinct predicates | 28 | **63,948** |
| Post-normalization distinct | 10 | **62,022** |
| Dropped (code tokens / operators / numbers) | 7 | **194** |
| Distinct collapse groups | — | **4,178** |
| Raw forms folded into a collapse | — | **5,610** |
| Negation rewrites (raw → `not_<base>`) | 4 | **3,781** |
| Cardinality reduction | 64.3% | **3.0%** |

### Honest read of the 3.0%

The live vocabulary is far worse than the issue's "~1000+" estimate — **63,948**
distinct predicates. The conservative module collapses the three *named*
contamination classes very effectively (negation alone folds 3,781 raw forms,
with families like `not_contains` absorbing 24 contraction variants), but those
classes are a **small slice** of the total. The dominant cost is the **long
tail of one-off verbose predicates** the LLM emitted (thousands of unique
multi-word relation strings) that a fixed `SYNONYM_MAP` does not touch by
design. So the headline reduction is only 3.0%.

The takeaway is the same one the issue's "possible directions" list anticipates:
post-hoc canonicalization cleans the *known* noise classes cheaply and safely,
but driving the cardinality down to "dozens, not thousands" requires either a
**closed-vocabulary extractor** (direction #3) or a **canonical-predicate
dictionary** (direction #2) — a larger change than this surface-form pass.

### Top collapse groups (live)

| Canonical | Raw forms folded in |
| --- | --- |
| `not_contains` | 24 |
| `not_run` | 15 |
| `not_fire` | 14 |
| `not_change` | 13 |
| `not_uses` | 13 |
| `is_a` | 12 (`is`, `are`, `was`, `was_a`, `is_an`, `is_an_instance_of`, `instance_of`, …) |

### Dropped tokens (live, sample)

Operators / merge markers / numeric noise the extractor bound as predicates —
all dropped: `!=`, `===`, `<<<<<<<`, `>>>>>>>`, `&&`, `=>`, `100644`, `404`, `2>`.
DOM/DB-API method names dropped: `appendchild`, `append_child`, `createelement`,
`executemany`, `fetchall`, `getelementbyid`, `getattribute`, `innerhtml`,
`classlist`. (194 dropped total.)

### Bundled-sample collapses (small offline demo)

| Canonical | Collapsed raw forms |
| --- | --- |
| `is_a` | `instance_of`, `is`, `is_an_instance_of`, `was_a` |
| `depends_on` | `depends_upon`, `requires` |
| `not_appear` | `'doesn't_appear'`, `does_not_appear` |
| `part_of` | `belongs_to`, `is_a_part_of` |
| `references` | `is_a_reference`, `refers_to` |

## Design notes

- **Conservative synonym collapse.** Only surface-form / tense / article
  variants of the *same* edge are merged. Semantically distinct relations stay
  separate (`part_of` is never folded into `is_a`; `created_by` never into
  `owned_by`). The full map is `SYNONYM_MAP` in `kg_predicate_norm.py`.
- **Negation as a polarity facet.** `does_not_*` / `doesn't_*` / `aren't_*` all
  peel to a uniform `not_<base>` after the base is canonicalized, so polarity
  is predictable rather than fanning out across contraction spellings.
- **Code-token drop is an explicit blocklist + a narrow digit heuristic.** A
  pure "drop single lowercase tokens" rule would kill legitimate verbs
  (`uses`, `owns`), so the blocklist is additive and the heuristic only flags
  single tokens containing a digit (`utf8decode`).

## Not wired into the write path

`normalize_predicate` is a pure module with no DB imports. It is **not** called
from the extraction/write path in this PR — wiring it in is a follow-up and
must be opt-in/guarded so a normalization regression can't silently corrupt new
triples. Today's extractor still uses `mempalace.kg_llm_extractor._normalize_predicate`
(lowercase + snake_case only); this module is the richer replacement candidate.
