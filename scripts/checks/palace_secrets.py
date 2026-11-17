#!/usr/bin/env python3
"""CHECK palace-secrets: does the palace hold a secret the miner's strong rules would take, and does the miner
still redact one on the way in?

Every write is redacted (mempalace.redact). This check measures both ends of that promise, daily:

1. **Miner positive control.** Fake credentials are built at run time (an AWS access key id, a GitHub token, an
   "sk-" key; never a literal in this file), written into a throwaway transcript, and mined through the daemon's
   own ``POST /mine`` into wing ``palace_probe``, the path every hook save takes. The check waits until the mine has
   filed every chunk (the drawers' ``chunk_total``), then judges THIS run's drawers only: none may hold a fake value,
   and each must have been replaced by exactly its own placeholder. Then the probe drawers and file are deleted, in a
   ``finally``, together with any a past run left; leftovers are reported. "miner redaction: ok | FAIL | not measured
   (<why>)": mining paused, or the probe not filed within ``PROBE_WAIT_S``, is not a pass.
2. **Scan.** Every drawer (shared and private: document, metadata values AND keys) and every graph Entity name is
   run through the STRONG passes only: the built-in provider shapes (aws, github, openai, litellm, digitalocean,
   jwt, pem, …), configured patterns, and vault needles. The broad shapes (token, hex, quoted, assignment) are not
   judged here: they redact identifiers too often to act on unseen (CHECK palace-secret-drawers counts them).
3. **Redact** (``PALACE_SECRETS_REDACT=1``): per table, one transaction locks the hit rows (``FOR UPDATE``), checks
   each is still the version the scan judged (md5 of document, metadata, embedding), writes those exact rows to a
   0600 backup, and updates them (re-embedded). Entity names are renamed, never deleted, after a 0600 backup of their
   properties, each by compare-and-set. Backups hold the secret and are pruned after ``BACKUP_DAYS`` days on every
   run. Without the switch the check only reports.

- **fail**: miner redaction FAIL, a hit that could not be redacted, or the scan could not run.
- **warn**: hits found (a secret reached the palace: its owner rotates it; wings and kinds are named, never a
  value), the positive control not measured, or probe leftovers.
- **ok**: 0 hits, miner redaction ok, nothing left behind.

Why (2026-11-16, realmwatch findings #489): nothing measured whether the miner still redacts, and the palace
held 24 drawers of private-key text no rule caught.
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import os
import secrets as _secrets
import string
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, label, line, read_var, run

NAME = "palace-secrets"
PROBE_WING = "palace_probe"
PROBE_WAIT_S = int(os.environ.get("PALACE_SECRETS_PROBE_WAIT_S", "1200"))
BACKUP_DAYS = 7
PAGE = 2000
TABLES = ("mempalace_drawers", "mempalace_private_drawers")
ROW_MD5 = "md5(coalesce(document, '')), md5(coalesce(metadata::text, '')), md5(coalesce(embedding::text, ''))"


# ---- the fakes (built at run time; this file holds no credential-shaped literal) ----


def make_fakes(rng=None) -> dict:
    """kind -> a fake value of that kind, random each run."""
    rng = rng or _secrets.SystemRandom()
    up = string.ascii_uppercase + string.digits
    an = string.ascii_letters + string.digits

    def pick(alphabet, n):
        return "".join(rng.choice(alphabet) for _ in range(n))

    return {
        "aws": "AK" + "IA" + pick(up, 16),
        "github": "gh" + "p_" + pick(an, 36),
        # mixed case and digits, so the litellm shape takes it whatever else is configured
        "litellm": "sk" + "-" + "Q7" + pick(an, 20) + "a1",
    }


def probe_transcript(fakes: dict, session: str) -> str:
    """A one-exchange Claude Code transcript (jsonl) that prints every fake."""
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    said = "Here are the values: " + " ".join(
        f"{k}={v}" for k, v in sorted(fakes.items())
    )
    rows = [
        {
            "type": "user",
            "sessionId": session,
            "timestamp": now,
            "uuid": _secrets.token_hex(8),
            "message": {
                "role": "user",
                "content": "palace-secrets probe: print the test values",
            },
        },
        {
            "type": "assistant",
            "sessionId": session,
            "timestamp": now,
            "uuid": _secrets.token_hex(8),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": said}],
            },
        },
    ]
    return "\n".join(json.dumps(r) for r in rows) + "\n"


def judge_probe(docs: list, fakes: dict, placeholder) -> tuple:
    """(state, detail) from THIS run's probe drawers' text (document + metadata). ok: no fake value anywhere, and
    each fake's own placeholder (its kind and the hash of its value) present. FAIL names kinds only."""
    if not docs:
        return "not measured", "no probe drawer"
    blob = "\n".join(docs)
    leaked = sorted(k for k, v in fakes.items() if v in blob)
    if leaked:
        return "FAIL", "raw " + ",".join(leaked)
    missing = sorted(k for k, v in fakes.items() if placeholder(k, v) not in blob)
    if missing:
        return "FAIL", "no placeholder for " + ",".join(missing)
    return "ok", f"{len(docs)} probe drawer(s)"


# ---- the strong passes ----


def strong_passes():
    """mempalace.redact's own passes: built-in provider shapes + configured patterns + vault needles. The broad
    #619 shapes, assignments and codes are separate steps of redact() and are not in here."""
    from mempalace import redact as R

    return R.patterns(), R._placeholder


def strong_redact(text, passes, placeholder) -> tuple:
    """(text with every strong run replaced, Counter of kinds replaced)."""
    kinds_hit = collections.Counter()
    if not text:
        return text, kinds_hit
    out = text
    for combined, kinds in passes:

        def sub(m, kinds=kinds):
            if m.group("_mprkeep") is not None:
                return m.group(0)
            kind = next(
                k for i, k in enumerate(kinds) if m.group(f"_mpr{i}") is not None
            )
            kinds_hit[kind] += 1
            return placeholder(kind, m.group(0))

        out = combined.sub(sub, out)
    return out, kinds_hit


def strong_redact_value(value, passes, placeholder) -> tuple:
    """A JSON value (metadata) with its strings strong-redacted at any depth, dict KEYS included; (value, Counter).
    A redacted key that collides with another key keeps both, the later one suffixed ``#2``, ``#3``…"""
    hit = collections.Counter()
    if isinstance(value, str):
        return strong_redact(value, passes, placeholder)
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            nk, kk = (
                strong_redact(key, passes, placeholder)
                if isinstance(key, str)
                else (key, collections.Counter())
            )
            hit += kk
            if nk in out:
                n = 2
                while f"{nk}#{n}" in out:
                    n += 1
                nk = f"{nk}#{n}"
            out[nk], k = strong_redact_value(v, passes, placeholder)
            hit += k
        return out, hit
    if isinstance(value, list):
        out = []
        for v in value:
            nv, k = strong_redact_value(v, passes, placeholder)
            out.append(nv)
            hit += k
        return out, hit
    return value, hit


# ---- the line ----


def safe_label(text, passes=None, placeholder=None) -> str:
    """A wing or kind for the line: checklib.label, and never a value a strong pass would take."""
    out = label(text)
    if passes is not None and strong_redact(out, passes, placeholder)[1]:
        return "<wing name holds a secret shape>"
    return out


def assess(
    hits: dict,
    redacted: int,
    failed: int,
    scanned: int,
    entities: int,
    ent_hits: int,
    miner: tuple,
    redact_on: bool,
    error: str | None = None,
    leftovers: int = 0,
    passes=None,
    placeholder=None,
) -> str:
    """hits: {(wing, kind): n} over drawers; miner: (state, detail); redacted/failed count drawers and entities."""
    mstate, mdetail = miner
    miner_txt = f"miner redaction: {mstate}" + (
        f" ({mdetail})" if mstate != "ok" else ""
    )
    left_txt = f"; {leftovers} probe leftover(s)" if leftovers else ""
    if error:
        return line(
            NAME,
            "fail",
            f"scan failed: {error}; {miner_txt}{left_txt}",
            "owner palace lane: fix the check",
        )
    n = sum(hits.values())
    by_wing = collections.defaultdict(collections.Counter)
    for (wing, kind), k in hits.items():
        by_wing[safe_label(wing, passes, placeholder)][kind] += k
    parts = [
        f"{w} ({','.join(f'{label(k)}×{c}' for k, c in sorted(ks.items()))})"
        for w, ks in sorted(by_wing.items(), key=lambda x: (-sum(x[1].values()), x[0]))
    ]
    where = "; ".join(parts[:6]) + (
        f"; +{len(parts) - 6} wings" if len(parts) > 6 else ""
    )
    fact = (
        f"{n} hits in drawers{': ' + where if where else ''}, {ent_hits} in entity names; {redacted} redacted"
        f"{', ' + str(failed) + ' NOT redacted' if failed else ''}; {scanned:,} drawers and {entities:,} entities "
        f"scanned; {miner_txt}{left_txt}"
    )
    if mstate == "FAIL" or failed:
        return line(
            NAME,
            "fail",
            fact,
            "owner palace lane: the miner stores secrets raw, or a hit stayed; fix first",
        )
    if n or ent_hits:
        nxt = "owner of each wing: rotate what was printed (palace lane names the drawers)"
        if not redact_on:
            nxt += "; palace lane: redact (PALACE_SECRETS_REDACT=1)"
        return line(NAME, "warn", fact, nxt)
    if mstate != "ok" or leftovers:
        return line(
            NAME, "warn", fact, "owner palace lane: the miner control did not run clean"
        )
    return line(NAME, "ok", fact)


# ---- the daemon and the probe ----


def _daemon(method, path, key, body=None, timeout=60):
    base = os.environ.get("PALACE_DAEMON_URL", "http://127.0.0.1:8085").rstrip("/")
    req = urllib.request.Request(
        base + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, (json.loads(raw) if raw else {})


def state_dir(sub: str) -> Path:
    root = Path(
        os.environ.get("PALACE_SECRETS_STATE_DIR") or "~/.local/state/palace-secrets"
    ).expanduser()
    d = root / sub
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(d, 0o700)
    return d


def probe_start(key, pdir: Path) -> tuple:
    """(fakes, path, early verdict or None): writes the probe transcript and queues its mine. The skip reason the
    daemon gives is never echoed (it is text from elsewhere): only "mining paused"."""
    fakes = make_fakes()
    session = _secrets.token_hex(16)
    path = pdir / f"{session}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(probe_transcript(fakes, session))
    try:
        status, out = _daemon(
            "POST",
            "/mine",
            key,
            {
                "dir": str(path),
                "wing": PROBE_WING,
                "mode": "convos",
                "background": True,
            },
        )
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return fakes, path, ("not measured", f"POST /mine failed: {type(exc).__name__}")
    if isinstance(out, dict) and out.get("skipped"):
        return fakes, path, ("not measured", "mining paused")
    if status not in (200, 202):
        return fakes, path, ("not measured", f"POST /mine answered {status}")
    return fakes, path, None


def _rows_for(conn, source: str):
    return conn.execute(
        "SELECT id, document, metadata::text, (metadata->>'chunk_total')::int FROM mempalace_drawers "
        "WHERE metadata->>'source_file' = %s",
        (source,),
    ).fetchall()


def probe_wait(conn, path: Path, fakes: dict, placeholder, deadline: float) -> tuple:
    """Wait until THIS run's probe has every chunk filed (count == chunk_total, stable across two reads), then
    judge it."""
    seen = None
    while time.time() < deadline:
        rows = _rows_for(conn, str(path))
        total = max((r[3] or 0 for r in rows), default=0)
        if rows and total and len(rows) >= total:
            if seen == len(rows):
                return judge_probe(
                    [f"{d}\n{m}" for _i, d, m, _t in rows], fakes, placeholder
                )
            seen = len(rows)
        time.sleep(10)
    return "not measured", f"probe not filed within {PROBE_WAIT_S // 60} min"


def probe_cleanup(conn, key, pdir: Path) -> int:
    """Delete every probe drawer (this run's and any a past run left) and every probe file; the number left."""
    left = 0
    try:
        rows = conn.execute(
            "SELECT id FROM mempalace_drawers WHERE metadata->>'source_file' LIKE %s",
            (str(pdir) + "/%",),
        ).fetchall()
    except Exception:  # noqa: BLE001 -- cannot see: count it as left
        return 1
    for (did,) in rows:
        try:
            status, _out = _daemon("DELETE", f"/memory/{did}", key)
            left += status != 200
        except (urllib.error.URLError, OSError, ValueError):
            left += 1
    for f in pdir.glob("*.jsonl"):
        try:
            f.unlink()
        except OSError:
            left += 1
    return left


# ---- scan and redact ----


def scan(conn, passes, placeholder):
    """Yield (table, id, wing, new_doc, new_meta, kinds, md5s) for every drawer a strong pass would change."""
    for table in TABLES:
        last = ""
        while True:  # keyset pages by primary key: short statements, no transaction held for the whole scan
            rows = conn.execute(
                f"SELECT id, wing, document, metadata, {ROW_MD5} FROM {table} WHERE id > %s ORDER BY id LIMIT %s",
                (last, PAGE),
            ).fetchall()
            if not rows:
                break
            last = rows[-1][0]
            for did, wing, doc, meta, *md5 in rows:
                nd, kd = strong_redact(doc or "", passes, placeholder)
                nm, km = strong_redact_value(meta, passes, placeholder)
                kinds = kd + km
                if kinds:
                    yield table, did, wing, nd, nm, kinds, tuple(md5)


def count_rows(conn) -> int:
    return sum(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES)


def prune_backups(bdir: Path) -> None:
    cutoff = time.time() - BACKUP_DAYS * 86400
    for old in list(bdir.glob("*.jsonl")):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass


def _backup_file(bdir: Path, name: str):
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fd = os.open(
        bdir / f"{stamp}-{name}.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
    )
    return os.fdopen(fd, "w", encoding="utf-8")


def redact_rows(conn, found, bdir: Path) -> tuple:
    """(redacted, failed). Per table, one transaction: lock the rows, check each is the version the scan judged,
    back up exactly those rows (0600), update them (re-embedded). A mismatch fails the table's batch."""
    from mempalace.backends.postgres import _embed

    done = bad = 0
    by_table = collections.defaultdict(list)
    for row in found:
        by_table[row[0]].append(row)
    for table, rows in by_table.items():
        vectors = list(_embed([r[3] for r in rows]))
        if len(vectors) != len(rows):
            bad += len(rows)
            continue
        want = {r[1]: r for r in rows}
        try:
            with conn.transaction():
                locked = conn.execute(
                    f"SELECT id, document, metadata::text, embedding::text, {ROW_MD5} FROM {table} "
                    "WHERE id = ANY(%s) ORDER BY id FOR UPDATE",
                    ([r[1] for r in rows],),
                ).fetchall()
                if len(locked) != len(rows) or any(
                    tuple(lk[4:]) != want[lk[0]][6] for lk in locked
                ):
                    raise RuntimeError("a row changed since the scan")
                with _backup_file(bdir, table) as f:
                    for did, doc, meta, emb, *_m in locked:
                        f.write(
                            json.dumps(
                                {
                                    "id": did,
                                    "document": doc,
                                    "metadata": meta,
                                    "embedding": emb,
                                }
                            )
                            + "\n"
                        )
                    f.flush()
                    os.fsync(f.fileno())
                for (_t, did, _w, nd, nm, _k, _md5), vec in zip(rows, vectors):
                    cur = conn.execute(
                        f"UPDATE {table} SET document = %s, metadata = %s::jsonb, embedding = %s::vector WHERE id = %s",
                        (
                            nd,
                            json.dumps(nm),
                            "[" + ",".join(f"{x:f}" for x in vec) + "]",
                            did,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("update touched no row")
            done += len(rows)
        except Exception:  # noqa: BLE001 -- the table's batch rolled back; counted and reported
            bad += len(rows)
    return done, bad


def entity_scan(conn, passes, placeholder, redact_on, bdir: Path) -> tuple:
    """(entities scanned, entity names a strong pass would change, renamed, failed). Renames only when redact_on,
    never deletes: a 0600 backup of each vertex's properties first, then compare-and-set on the full properties."""
    conn.execute("LOAD 'age'")
    conn.execute('SET search_path = ag_catalog, "$user", public')
    n = hit = done = bad = 0
    renames = []
    # Entity has no btree on id (only a GIN on properties): stream it once, in single-row mode (no named cursor, no
    # transaction held), rather than page it with a sort per page.
    cur = conn.cursor()
    for vid, props in cur.stream(
        'SELECT id::text, properties::text FROM mempalace_kg."Entity"'
    ):
        n += 1
        try:
            name = json.loads(props).get("name")
        except ValueError:
            continue
        if isinstance(name, str):
            new, k = strong_redact(name, passes, placeholder)
            if k:
                hit += 1
                renames.append((vid, props, new))
    if redact_on and renames:
        with _backup_file(bdir, "entity") as f:
            for vid, props, _new in renames:
                f.write(json.dumps({"id": vid, "properties": props}) + "\n")
            f.flush()
            os.fsync(f.fileno())
        for vid, props, new in renames:
            p = json.loads(props)
            p["name"] = new
            try:
                c = conn.execute(
                    'UPDATE mempalace_kg."Entity" SET properties = %s::agtype '
                    "WHERE id = %s::graphid AND properties::text = %s",
                    (json.dumps(p), vid, props),
                )
                ok = c.rowcount == 1
            except Exception:  # noqa: BLE001 -- counted as not redacted
                ok = False
            done += ok
            bad += not ok
    conn.execute("SET search_path = public")
    return n, hit, done, bad


def body() -> None:
    try:
        import psycopg
    except ImportError as exc:
        emit(
            NAME,
            line(
                NAME,
                "fail",
                f"cannot import {exc.name} (run with the palace-daemon venv)",
            ),
        )
        return
    env_file = Path(
        os.environ.get("PALACE_CHECKS_DSN_FILE") or "~/.config/palace-daemon/env"
    ).expanduser()
    dsn = read_var(env_file, "MEMPALACE_POSTGRES_DSN")
    key = read_var(env_file, "PALACE_API_KEY")
    if not dsn or not key:
        emit(
            NAME,
            line(
                NAME,
                "fail",
                "no DSN or API key in the daemon env file",
                "owner palace lane",
            ),
        )
        return
    redact_on = os.environ.get("PALACE_SECRETS_REDACT") == "1"
    pdir, bdir = state_dir("probe"), state_dir("backup")
    prune_backups(bdir)
    hits = collections.Counter()
    redacted = failed = scanned = ents = ent_hits = leftovers = 0
    error = None
    passes = placeholder = None
    verdict = ("not measured", "the check stopped first")
    conn = None
    try:
        passes, placeholder = strong_passes()
        fakes, path, early = probe_start(key, pdir)
        deadline = time.time() + PROBE_WAIT_S
        conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
        conn.execute(
            "SET statement_timeout = '120s'"
        )  # per statement; the scan is many short pages
        found = list(scan(conn, passes, placeholder))
        scanned = count_rows(conn)
        for _t, _d, wing, _nd, _nm, kinds, _m in found:
            for kind, k in kinds.items():
                hits[(wing, kind)] += k
        ents, ent_hits, e_done, e_bad = entity_scan(
            conn, passes, placeholder, redact_on, bdir
        )
        if redact_on and found:
            redacted, failed = redact_rows(conn, found, bdir)
        redacted, failed = redacted + e_done, failed + e_bad
        verdict = early or probe_wait(conn, path, fakes, placeholder, deadline)
    except Exception as exc:  # noqa: BLE001 -- a check reports, it never raises
        error = type(exc).__name__
    finally:
        if conn is None:
            try:
                conn = psycopg.connect(dsn, connect_timeout=20, autocommit=True)
            except Exception:  # noqa: BLE001
                conn = None
        leftovers = probe_cleanup(conn, key, pdir) if conn is not None else 1
        if conn is not None:
            conn.close()
    emit(
        NAME,
        assess(
            hits,
            redacted,
            failed,
            scanned,
            ents,
            ent_hits,
            verdict,
            redact_on,
            error,
            leftovers,
            passes,
            placeholder,
        ),
    )


def main() -> int:
    return run(NAME, body)


if __name__ == "__main__":
    sys.exit(main())
