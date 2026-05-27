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

Dry-run report against the **live vocabulary** (READ-ONLY — `graph_stats` is a
pure read; the script never writes the graph):

```bash
set -a; source ~/.config/palace-daemon/env; set +a
venv/bin/python scripts/predicate_norm_report.py --live
# or, decoupled from the daemon being up at report time:
curl -sS -H "X-Api-Key: $PALACE_API_KEY" -H "Content-Type: application/json" \
     "$PALACE_DAEMON_URL/mcp" \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/call",
          "params":{"name":"mempalace_graph_stats","arguments":{}}}' \
  | jq -r '.result.content[0].text' > vocab.json
venv/bin/python scripts/predicate_norm_report.py --vocab-file vocab.json
```

## Live cardinality — UNAVAILABLE at authoring time

The issue's reproduction probe (`familiar.jphe.in:8085` `graph_stats`) could
**not** be run while building this PR: the daemon's HTTP port (8085) was closed
(connection refused; host pingable via Tailscale but no listener), and SSH to
the host was blocked by a changed host key (strict checking refused — not
auto-cleared, security). The before/after numbers below are therefore against
the **bundled issue-#50 sample**, not the 274,609-entity production corpus.

Re-run `--live` (or `--vocab-file`) once the daemon is reachable to capture the
true production cardinality reduction.

## Results (bundled sample)

| Metric | Value |
| --- | --- |
| Original distinct predicates | 28 |
| Post-normalization distinct predicates | 10 |
| Dropped (code tokens) | 7 |
| Cardinality reduction | 64.3% |

### Top collapses (raw → canonical)

| Canonical | Collapsed raw forms |
| --- | --- |
| `is_a` | `instance_of`, `is`, `is_an_instance_of`, `was_a` |
| `depends_on` | `depends_upon`, `requires` |
| `not_appear` | `'doesn't_appear'`, `does_not_appear` |
| `part_of` | `belongs_to`, `is_a_part_of` |
| `references` | `is_a_reference`, `refers_to` |
| `not_adapt` | `don't_adapt` |
| `not_merged` | `aren't_merged` |

### Dropped code tokens

`appendchild`, `createelement`, `executemany`, `fetchall`, `getelementbyid`,
`queryselector`, `setattribute`

### Negation rewrites (raw → `not_<base>`)

| Raw | Normalized |
| --- | --- |
| `'doesn't_appear'` | `not_appear` |
| `aren't_merged` | `not_merged` |
| `does_not_appear` | `not_appear` |
| `don't_adapt` | `not_adapt` |

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
