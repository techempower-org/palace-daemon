# Recovery: ChromaDB segment quarantined with `dimensionality: None`

**Symptom** — On daemon startup (or any first-access of a chromadb collection),
the integrity gate quarantines a segment dir with this log line:

```
Quarantined invalid HNSW metadata in /path/to/palace/<uuid>:
  labels present but dimensionality is missing or invalid (None)
```

ChromaDB falls back to creating a fresh empty segment dir under the same UUID,
losing access to all vectors that were persisted in the now-quarantined dir.

**Cause** — ChromaDB 1.5.x has a save/load asymmetry bug in
`local_persistent_hnsw.py`: it sometimes serializes `_persist_data` as a raw
`dict` (not a `PersistentData` class instance) AND fails to populate the
`dimensionality` field. Other fields (`total_elements_added`, `id_to_label`,
`label_to_id`) are written correctly. On reload, `PersistentData.dimensionality`
comes back as `None`, the integrity gate calls the segment invalid, and the
data files (`data_level0.bin`, `link_lists.bin`, `length.bin`) become orphans.

**Observed empirically** on disks on 2026-05-13 after a 10.4h
`mempalace.repair.rebuild_index` run. The rebuild itself succeeded — it
wrote 150,000 vectors to `data_level0.bin` and produced a valid label map —
but the index metadata file had `dimensionality=None`. Daemon quarantined
both rebuilt segment dirs on next startup. Recovery (below) brought back
99.97% of the recall in ~90 seconds.

---

## Prerequisites

- The quarantined directory is on disk (`<uuid>.corrupt-<timestamp>` next to
  the original UUID path). Don't delete `.corrupt-*` dirs without checking
  the chromadb index metadata file first — that's where recoverable state
  lives.
- chromadb is installed in your venv (we need to construct `PersistentData`
  via its actual class).
- The palace-daemon process is **stopped** for the duration of the patch.
  Two `PersistentClient` instances against the same palace deadlock on the
  sqlite filelock.

## Diagnose

```bash
# Find the quarantine event in the daemon log
sudo journalctl -u palace-daemon | grep "Quarantined invalid HNSW metadata"

# Inspect the quarantined chromadb metadata file
/path/to/venv/bin/python3 - <<'PYEOF'
import glob, os
from pickle import load  # chromadb's persist file is loaded with stdlib pickle
for d in glob.glob('/path/to/palace/*.corrupt-*'):
    meta = os.path.join(d, 'index_metadata.pickle')
    if not os.path.isfile(meta):
        continue
    with open(meta, 'rb') as f:
        data = load(f)
    print(f'--- {d} ---')
    print(f'  type: {type(data).__name__}')
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict):
                print(f'  {k}: dict[{len(v)}]')
            else:
                print(f'  {k}: {v!r}'[:120])
PYEOF
```

If the output shows `type: dict` and `dimensionality: None`, you have this
exact bug. If `total_elements_added` is a meaningful count (e.g. 150000)
and `id_to_label` is a non-empty dict, recovery is straightforward.

If both `id_to_label` and `label_to_id` are empty, the segment is genuinely
empty — no recovery possible from this file, treat as a normal restart.

## Recover

```bash
# 1. Stop the daemon so it releases the sqlite filelock
sudo systemctl stop palace-daemon.service

# 2. (Optional) Snapshot the sqlite db as a rollback path
cp /path/to/palace/chroma.sqlite3 /path/to/palace/chroma.sqlite3.pre-recovery-$(date +%Y%m%d-%H%M%S)

# 3. Move any "fresh empty" replacement segment dir aside.
#    ChromaDB creates these when it can't load a quarantined segment.
#    They have <1000 elements and aren't what you want kept.
STAMP=$(date +%Y%m%d-%H%M%S)
mv /path/to/palace/<uuid> /path/to/palace/<uuid>.preserved-fresh-$STAMP

# 4. Restore the .corrupt-<timestamp> dir back to the live UUID path
mv /path/to/palace/<uuid>.corrupt-<timestamp> /path/to/palace/<uuid>

# 5. Patch the chromadb metadata file — convert dict to PersistentData
#    class instance with the missing dimensionality field populated.
/path/to/venv/bin/python3 - <<'PYEOF'
import os
import shutil
from pickle import dump, load  # chromadb persist files use stdlib pickle

SEGDIR = '/path/to/palace/<uuid>'
META = os.path.join(SEGDIR, 'index_metadata.pickle')
DIMENSIONALITY = 384  # standard mempalace; check your collection's hnsw:space metadata

with open(META, 'rb') as f:
    data = load(f)

from chromadb.segment.impl.vector.local_persistent_hnsw import PersistentData

if isinstance(data, dict):
    fixed = PersistentData(
        dimensionality=DIMENSIONALITY,
        total_elements_added=data['total_elements_added'],
        id_to_label=data['id_to_label'],
        label_to_id=data['label_to_id'],
        id_to_seq_id=data.get('id_to_seq_id', {}),
    )
else:
    fixed = data
    fixed.dimensionality = DIMENSIONALITY

# Backup the broken file first
shutil.copy2(META, META + '.broken-backup')

with open(META, 'wb') as f:
    dump(fixed, f)

# Verify the write
with open(META, 'rb') as f:
    verify = load(f)
print(f'patched: type={type(verify).__name__}, '
      f'dimensionality={verify.dimensionality}, '
      f'elements={verify.total_elements_added}')
PYEOF

# 6. Start the daemon
sudo systemctl start palace-daemon.service

# 7. Verify HNSW count vs sqlite count matches expectations
/path/to/venv/bin/python3 -c '
import chromadb
from chromadb.segment import SegmentManager, VectorReader
c = chromadb.PersistentClient(path="/path/to/palace")
col = c.get_collection("mempalace_drawers")
seg = c._server._system.require(SegmentManager).get_segment(col.id, VectorReader)
print(f"HNSW={seg._total_elements_added}, Sqlite={col.count()}")
'
```

A small gap (a few dozen drawers) between HNSW and sqlite is normal — those
are recent writes queued for the next HNSW flush at `sync_threshold` boundary.
A gap of thousands suggests the recovery captured a mid-rebuild state; consider
running `rebuild_index` (carefully — see palace-daemon#9 fix) for those.

## Cleanup after recovery succeeds

```bash
# Confirm vector search returns real similarity scores (not BM25 fallback)
curl -sS "http://localhost:8085/search?q=test&limit=1" \
  -H "X-API-Key: $PALACE_API_KEY" | jq '.fallback, .results[0].similarity'
# fallback should be null; similarity should be a real float

# Once confirmed working, the preserved/backup files are safe to delete:
rm -rf /path/to/palace/<uuid>.preserved-fresh-*
rm /path/to/palace/<uuid>/index_metadata.pickle.broken-backup
# Keep the chroma.sqlite3.pre-recovery-* snapshot for at least a few days
```

## Why this works

The data files (`data_level0.bin`, `link_lists.bin`, `length.bin`) contain
the actual vector embeddings + HNSW graph structure. They're intact and
correct — only the small metadata file that records `dimensionality` is
broken. ChromaDB needs that field to know how to interpret the vector
bytes, but the field is constant for a given collection (set when the
collection is created with `hnsw:space` metadata). We can supply it
externally and the rest of the segment loads normally.

The `id_to_label` and `label_to_id` dicts ARE preserved in the broken
file, so the recovered segment knows which embedding_id corresponds to
each HNSW label. That's what makes search results map back to drawers
correctly.

## Related

- Upstream chromadb bug: `_persist_data` should always be saved as a
  `PersistentData` instance with all fields populated, never as a dict
  with `dimensionality=None`. TODO: file at `chroma-core/chroma`.
- See also: palace-daemon `#10` (hnswlib import guard — distinct bug shape
  but same family of "chromadb silently produces unusable state")
- See also: palace-daemon `#9` (`/repair?mode=rebuild` deadlock — the
  rebuild path that historically produced these broken segments because the
  daemon's cached client wasn't released before rebuild_index opened a
  second one)

## Confidence

Recovered 99.97% of recall on a 183k-drawer palace using this exact procedure
on 2026-05-13. Total runtime: ~90 seconds from "stop daemon" to "vector
search returning real similarity scores."
