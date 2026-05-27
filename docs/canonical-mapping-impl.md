# Canonical Predicate Mapping — Approach (a) Implementation (issue #72)

Builds on the #74 spike. **No production graph mutation is performed by this PR.**

Approach (a) = post-extraction canonical mapping: map each emitted predicate to
the 39-relation closed ontology (`kg_canonical_vocab`) *before* the RELATION
edge is persisted, retaining the raw string for reversibility. Two deliverables:

1. A guarded write-path **seam** (`kg_canonical_writepass.py`).
2. A one-shot **migration script** (`scripts/canonical_migration.py`), default
   dry-run, `--apply` gated.

## Architectural finding: the RELATION write path is in mempalace, not the daemon

Investigated per the task. The daemon **only reads** the knowledge graph. New
RELATION triples are extracted and persisted by the **mempalace package** —
`mempalace/kg_triple_worker.py`, which calls `extract_triples`
(`kg_llm_extractor`) and then `add_triple(subject, predicate, object, …)` with
`relation_type: $rt`. That worker runs as a **separate process**
(`python -m mempalace …`); palace-daemon never imports or launches it.

Consequences:

* palace-daemon **cannot intercept the write** from its own process. So this PR
  ships the **seam** (the pure, guarded mapping decision the writer should call)
  and its tests in palace-daemon, and treats *wiring it into
  `kg_triple_worker`* as the upstream/(b) piece — a separate one-line,
  default-OFF change in the `techempower-org/mempalace` repo. Coupling mempalace
  to palace-daemon (backwards dependency) is deliberately avoided.
* The **migration**, by contrast, is something the daemon fully mediates: it can
  enumerate the existing vocabulary read-only via `/cypher`. That part is
  complete here.

### Upstream wiring (the one-line seam, for the mempalace PR)

In `kg_triple_worker`'s write loop, the canonical decision drops in just before
`add_triple`:

```python
# in mempalace, importing the same decision logic (or a copy of it):
mp = map_for_write(t.predicate)          # default OFF → mp.relation_type == t.predicate
if mp.dropped:
    continue                             # skip code-token triples (flag ON only)
await kg.add_triple(
    t.subject, mp.relation_type, t.object,
    source=f"drawer:{drawer.drawer_id}", valid_from=t.valid_from,
    # + persist mp.raw_relation_type as an edge property when not None
)
```

## Guard: `PALACE_KG_CANONICAL_MAPPING` (default OFF)

`kg_canonical_writepass.map_for_write(raw)` reads the flag live:

| Flag | Behavior |
| --- | --- |
| unset / `0` / `false` / `off` (default) | **pass-through** — `relation_type == raw`, no `raw_relation_type`, nothing dropped. Byte-for-byte current behavior. |
| `1` / `true` / `on` | map to canonical (or `other`); retain original as `raw_relation_type`; code tokens flagged `dropped`. |

Merging this PR changes **nothing** in production until JP flips the flag. The
embedding mapper loads lazily, so a process that never enables it pays no
model-load cost.

## Reversibility

When mapping changes a predicate, the original is stored on the edge as
`raw_relation_type`. Rollback is a single pass:

```cypher
MATCH ()-[r:RELATION]->() WHERE r.raw_relation_type IS NOT NULL
  SET r.relation_type = r.raw_relation_type REMOVE r.raw_relation_type
```

## Dry-run migration (read-only) against production

`scripts/canonical_migration.py` reads the live vocabulary + frequencies over
READ-ONLY `/cypher` (`MATCH ()-[r:RELATION]->() RETURN r.relation_type, count(*)`),
maps each predicate, and reports the would-change plan.

Corpus: **1,060,950 entities; 1,724,791 RELATION triples; 64,029 distinct predicates.**

### Would-change numbers (embedding mode, threshold 0.45)

<!-- EMBED_DRYRUN -->

For reference, the **lexical** fallback (no embedding model) on the same data:

| Metric | Lexical |
| --- | --- |
| distinct after migration | 40 |
| edges would change | 1,472,163 (85.4%) |
| distinct remap rules | 63,794 |
| `other` bucket | 740,882 edges (62,247 distinct raws) |
| code tokens (left as-is) | 9,102 edges (195 distinct) |

The high "would change" % is expected: the two largest predicates alone
(`is`→`is_a` 350k, `has`→`contains` 245k) differ from their canonical, so most
*edges* get a (small, deterministic) relabel even though the *distinct* rule
count is what collapses the vocabulary.

## `--apply` is gated (NOT run here)

`/cypher` is read-only by construction (rejects write verbs with 403), so apply
**cannot** go through the daemon HTTP surface. `--apply` requires a **direct
postgres DSN** (`--dsn` / `MEMPALACE_POSTGRES_DSN`) — i.e. run on the daemon host
with the single-writer daemon paused and a fresh backup — and additionally
refuses without `--i-have-a-backup`. Even then, this PR's apply path is
intentionally inert (raises with the rewrite rules in hand) so the real
migration is gated on **JP + a graph backup**, coordinated separately.

**NO mutation was performed. `--apply` is gated on backup + JP go.**

## Reproduce

```bash
venv/bin/python -m pytest tests/test_kg_canonical_writepass.py tests/test_canonical_migration.py -q

set -a; source ~/.config/palace-daemon/env; set +a
venv/bin/python scripts/canonical_migration.py            # dry-run, embedding
venv/bin/python scripts/canonical_migration.py --lexical  # dry-run, no model
```
