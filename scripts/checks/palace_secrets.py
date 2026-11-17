#!/usr/bin/env python3
"""CHECK palace-secrets: does the palace hold a secret the miner's strong rules would take, and does the miner
still redact one on the way in?

Every write is redacted (mempalace.redact). This check measures both ends of that promise, daily:

1. **Miner positive control.** Fake credentials are built at run time (an AWS access key id, a GitHub token, an
   "sk-" key; never a literal in this file), written into a throwaway transcript, and mined through the daemon's
   own ``POST /mine`` into wing ``palace_probe``, the path every hook save takes. The probe drawers must hold
   none of the fake values and a placeholder for each kind. They are deleted afterwards (``DELETE /memory/{id}``),
   with the probe file. "miner redaction: ok | FAIL | not measured (<why>)": mining paused, or the probe not mined
   within ``PROBE_WAIT_S``, is not a pass.
2. **Scan.** Every drawer (shared and private: document and metadata) and every graph Entity name is run through
   the STRONG passes only: the built-in provider shapes (aws, github, openai, litellm, digitalocean, jwt, pem, …),
   configured patterns, and vault needles. The broad shapes (token, hex, quoted, assignment) are not judged here:
   they redact identifiers too often to act on unseen (CHECK palace-secret-drawers counts them).
3. **Redact** (``PALACE_SECRETS_REDACT=1``): each hit is replaced with its placeholder by compare-and-set on
   md5(document, metadata, embedding), re-embedded, after a 0600 COPY backup of the rows as read (it holds the
   secret; kept ``BACKUP_DAYS`` days, then pruned). Entities are renamed, never deleted. Without the switch the
   check only reports.

- **fail**: miner redaction FAIL, or a hit that could not be redacted (or the scan could not run).
- **warn**: hits found (a secret reached the palace: its owner rotates it; wings and kinds are named, never a
  value), or the positive control was not measured.
- **ok**: 0 hits and miner redaction ok.

Why (2026-11-16, realmwatch findings #489): nothing measured whether the miner still redacts, and the palace
held 24 drawers of private-key text no rule caught.
"""

from __future__ import annotations

import collections
import datetime as dt
import hashlib
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
ROW_MD5_EQ = (
    "md5(coalesce(document, '')) = %s AND md5(coalesce(metadata::text, '')) = %s "
    "AND md5(coalesce(embedding::text, '')) = %s"
)


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


def judge_probe(docs: list, fakes: dict) -> tuple:
    """(state, detail) from the probe drawers' text (document + metadata, as strings). ok: no fake value anywhere,
    and a placeholder for every kind. FAIL: a fake value is present (named by kind only)."""
    if not docs:
        return "not measured", "no probe drawer"
    blob = "\n".join(docs)
    leaked = sorted(k for k, v in fakes.items() if v in blob)
    if leaked:
        return "FAIL", "raw " + ",".join(leaked)
    missing = sorted(k for k in fakes if f"[redacted {k} sha256:" not in blob)
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
    """A JSON value (metadata) with its strings strong-redacted at any depth; (value, Counter)."""
    hit = collections.Counter()
    if isinstance(value, str):
        out, k = strong_redact(value, passes, placeholder)
        return out, k
    if isinstance(value, dict):
        out = {}
        for key, v in value.items():
            out[key], k = strong_redact_value(v, passes, placeholder)
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
) -> str:
    """hits: {(wing, kind): n} over drawers; miner: (state, detail)."""
    mstate, mdetail = miner
    miner_txt = f"miner redaction: {mstate}" + (
        f" ({mdetail})" if mstate != "ok" else ""
    )
    if error:
        return line(
            NAME,
            "fail",
            f"scan failed: {error}; {miner_txt}",
            "owner palace lane: fix the check",
        )
    n = sum(hits.values())
    by_wing = collections.defaultdict(collections.Counter)
    for (wing, kind), k in hits.items():
        by_wing[wing][kind] += k
    parts = [
        f"{label(w)} ({','.join(f'{label(k)}×{c}' for k, c in sorted(ks.items()))})"
        for w, ks in sorted(by_wing.items(), key=lambda x: (-sum(x[1].values()), x[0]))
    ]
    where = "; ".join(parts[:6]) + (
        f"; +{len(parts) - 6} wings" if len(parts) > 6 else ""
    )
    fact = (
        f"{n} hits in drawers{': ' + where if where else ''}, {ent_hits} in entity names; {redacted} redacted"
        f"{', ' + str(failed) + ' NOT redacted' if failed else ''}; {scanned:,} drawers and {entities:,} entities "
        f"scanned; {miner_txt}"
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
    if mstate != "ok":
        return line(
            NAME, "warn", fact, "owner palace lane: the miner control did not run"
        )
    return line(NAME, "ok", fact)


# ---- the daemon ----


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


def probe_dir() -> Path:
    d = Path(
        os.environ.get("PALACE_SECRETS_PROBE_DIR")
        or "~/.local/state/palace-secrets/probe"
    ).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def _probe_rows(conn, pdir: Path):
    return conn.execute(
        "SELECT id, document, metadata::text FROM mempalace_drawers WHERE metadata->>'source_file' LIKE %s",
        (str(pdir) + "/%",),
    ).fetchall()


def probe_start(key, pdir: Path) -> tuple:
    """(fakes, path, state or None): writes the probe transcript and queues its mine."""
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
        return (
            fakes,
            path,
            (
                "not measured",
                "mining paused: " + label(str(out.get("reason", ""))[:40]),
            ),
        )
    if status not in (200, 202):
        return fakes, path, ("not measured", f"POST /mine answered {status}")
    return fakes, path, None


def probe_finish(conn, key, pdir: Path, fakes, path, deadline) -> tuple:
    rows = []
    while time.time() < deadline:
        rows = [r for r in _probe_rows(conn, pdir) if r[0]]
        if rows:
            time.sleep(15)  # let the mine write every chunk
            rows = _probe_rows(conn, pdir)
            break
        time.sleep(15)
    verdict = (
        judge_probe([f"{d}\n{m}" for _i, d, m in rows], fakes)
        if rows
        else ("not measured", f"probe not mined within {PROBE_WAIT_S // 60} min")
    )
    probe_cleanup(conn, key, pdir)
    try:
        path.unlink()
    except OSError:
        pass
    return verdict


def probe_cleanup(conn, key, pdir: Path) -> None:
    """Delete every probe drawer (this run's, and any a past run left)."""
    for did, _d, _m in _probe_rows(conn, pdir):
        try:
            _daemon("DELETE", f"/memory/{did}", key)
        except (urllib.error.URLError, OSError, ValueError):
            pass


# ---- scan and redact ----


def scan(conn, passes, placeholder):
    """Yield (table, id, wing, new_doc, new_meta, kinds, md5s) for every drawer a strong pass would change."""
    for table in TABLES:
        last = ""
        while True:  # keyset pages by primary key: short statements, no transaction held open for the whole scan
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
    pdir = probe_dir()
    fakes, path, early = probe_start(key, pdir)
    deadline = time.time() + PROBE_WAIT_S
    hits, redacted, failed, scanned, ents, ent_hits, error = (
        collections.Counter(),
        0,
        0,
        0,
        0,
        0,
        None,
    )
    try:
        passes, placeholder = strong_passes()
        with psycopg.connect(dsn, connect_timeout=20, autocommit=True) as conn:
            conn.execute(
                "SET statement_timeout = '0'"
            )  # a streaming scan; the unit's timeout bounds it
            found = []
            for table, did, wing, nd, nm, kinds, md5 in scan(conn, passes, placeholder):
                if not did.startswith(f"drawer_{PROBE_WING}_"):
                    found.append((table, did, wing, nd, nm, kinds, md5))
            scanned = count_rows(conn)
            for _t, _d, wing, _nd, _nm, kinds, _m in found:
                for kind, k in kinds.items():
                    hits[(wing, kind)] += k
            ents, ent_hits = entity_scan(conn, passes, placeholder, redact_on)
            if redact_on and found:
                redacted, failed = redact_rows(conn, found)
            verdict = early or probe_finish(conn, key, pdir, fakes, path, deadline)
    except Exception as exc:  # noqa: BLE001 -- a check reports, it never raises
        error = type(exc).__name__
        verdict = early or ("not measured", "the scan failed first")
    emit(
        NAME,
        assess(
            hits, redacted, failed, scanned, ents, ent_hits, verdict, redact_on, error
        ),
    )


def entity_scan(conn, passes, placeholder, redact_on) -> tuple:
    """(entities scanned, entity names a strong pass would change); renames them when redact_on (never deletes)."""
    conn.execute("LOAD 'age'")
    conn.execute('SET search_path = ag_catalog, "$user", public')
    n = hit = 0
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
    if redact_on:
        for vid, props, new in renames:
            p = json.loads(props)
            p["name"] = new
            conn.execute(
                'UPDATE mempalace_kg."Entity" SET properties = %s::agtype WHERE id = %s::graphid AND properties::text = %s',
                (json.dumps(p), vid, props),
            )
    conn.execute("SET search_path = public")
    return n, hit


def redact_rows(conn, found) -> tuple:
    """(redacted, failed): backup, re-embed, compare-and-set, one transaction per table."""
    from mempalace.backends.postgres import _embed

    bdir = Path(
        os.environ.get("PALACE_SECRETS_BACKUP_DIR")
        or "~/.local/state/palace-secrets/backup"
    ).expanduser()
    bdir.mkdir(parents=True, exist_ok=True)
    os.chmod(bdir, 0o700)
    cutoff = time.time() - BACKUP_DAYS * 86400
    for old in bdir.glob("*.copy"):
        if old.stat().st_mtime < cutoff:
            old.unlink()
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    done = bad = 0
    by_table = collections.defaultdict(list)
    for row in found:
        by_table[row[0]].append(row)
    for table, rows in by_table.items():
        ids = [r[1] for r in rows]
        fd = os.open(
            bdir / f"{stamp}-{table}.copy", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        h = hashlib.sha256()
        with (
            os.fdopen(fd, "wb") as f,
            conn.cursor().copy(
                f"COPY (SELECT id, document, metadata, embedding FROM {table} WHERE id = ANY(%s) ORDER BY id) TO STDOUT",
                (ids,),
            ) as cp,
        ):
            for chunk in cp:
                f.write(chunk)
                h.update(chunk)
        vectors = list(_embed([r[3] for r in rows]))
        if len(vectors) != len(rows):
            bad += len(rows)
            continue
        try:
            with conn.transaction():
                for (_t, did, _w, nd, nm, _k, md5), vec in zip(rows, vectors):
                    cur = conn.execute(
                        f"UPDATE {table} SET document = %s, metadata = %s::jsonb, embedding = %s::vector "
                        f"WHERE id = %s AND {ROW_MD5_EQ}",
                        (
                            nd,
                            json.dumps(nm),
                            "[" + ",".join(f"{x:f}" for x in vec) + "]",
                            did,
                            *md5,
                        ),
                    )
                    if cur.rowcount != 1:
                        raise RuntimeError("row changed since read")
            done += len(rows)
        except Exception:  # noqa: BLE001 -- the whole table's batch rolled back; counted, reported
            bad += len(rows)
    return done, bad


def main() -> int:
    return run(NAME, body)


if __name__ == "__main__":
    sys.exit(main())
