# Versioning — TechEmpower fork of palace-daemon

## TL;DR

This fork uses **PEP 440 local version identifiers**:

    <upstream_base>+te.<N>

e.g. `1.8.1+te.1` — "TechEmpower fork build 1, sitting on top of everything in
upstream `rboarescu/palace-daemon` v1.8.1."

- `<upstream_base>` — the highest upstream release this fork is a verified
  **superset** of.
- `+te.<N>` — the TechEmpower fork build counter. Increments once per fork
  release; resets to `1` when `<upstream_base>` advances.

## Why not plain SemVer?

The fork previously reused upstream's `v1.x.y` SemVer namespace directly (last
such version: `1.9.1`). That created three problems:

1. **Tag collision.** Fork tags `v1.5.1`, `v1.7.0`–`v1.7.3` are the *same
   strings* upstream also emits, with *different content*. `git`, CI, and
   humans can't tell which lineage a bare `v1.7.2` belongs to, and a future
   upstream `v1.9.x` would collide with the fork's own `v1.9.0`/`v1.9.1`.
2. **No base linkage.** Nothing in the version recorded *which* upstream
   release the fork was built on.
3. **False linearity.** `1.9.1 > 1.8.1` reads as "one minor ahead on the same
   line." In reality the fork is a *diverged* line (265 commits past the
   pre-v1.5.1 merge-base) that has *contributed* its v1.8.x features back
   upstream (PRs #23 / #24 / #30). It was never "one minor ahead" — it was
   orthogonal.

`<upstream_base>+te.<N>` fixes all three: the base is in the string, `+te.*`
never collides (upstream never emits it), and the shape reads "diverged fork
build on top of upstream X," not "next in upstream's line."

Local-version identifiers are valid PEP 440 (Python) and valid SemVer build
metadata (`+…`). palace-daemon is a flat-file service, not a published PyPI
package, so no publish tooling rejects the local segment. They sort correctly:
`1.8.1 < 1.8.1+te.1 < 1.8.1+te.2`.

## Where it lives

- `main.py` — `VERSION` is the single source of truth (kept a **string
  literal** so `scripts/deploy.sh`'s regex can parse it); `UPSTREAM_BASE` is
  *derived* from it (`VERSION.split("+")[0]`, so the two can't drift) and `FORK`
  names the fork.
- `GET /health` payload —
  `{"version": "1.8.1+te.1", "fork": "techempower", "upstream_base": "1.8.1"}`.

## How to bump

**Fork-only change** (a feature/fix that isn't an upstream sync):
bump the `+te.<N>` counter in the `VERSION` literal — `1.8.1+te.1` →
`1.8.1+te.2`.

**After verifying parity with a newer upstream release:**
set the base part of the `VERSION` literal to the new upstream version and reset
the counter — `1.8.1+te.4` → `1.9.0+te.1` (`UPSTREAM_BASE` follows automatically,
since it's derived). "Verifying parity" means confirming the fork contains (or
supersedes) everything in that upstream release. Because features flow
fork→upstream here, parity is usually already true; the bump just records it.

**Tags:** `te/v<version>`, e.g. `te/v1.8.1+te.1`. The `te/` prefix keeps the
fork's tag namespace disjoint from upstream's `v*` tags in the same clone (both
remotes are fetched). Going forward only — historical `v1.*` fork tags are left
as-is, because re-tagging rewrites shared history.

## Relationship to upstream (important)

Features flow **fork → upstream**, not the other way. This fork is where
crash-loop detection, verified backups, the `/mine` backend guard, the
postgres/pgvector+AGE backends, hybrid/age-fused search, and silent-save were
built; several were contributed upstream via PR. A `git merge upstream/main` is
therefore **not** a routine catch-up — see [`upstream-sync.md`](upstream-sync.md)
for the standing assessment and the rare cases where cherry-picking a
genuinely-upstream-origin fix makes sense.
