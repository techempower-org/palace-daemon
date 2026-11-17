#!/usr/bin/env python3
"""palace-secrets classify: describe each CHECK palace-secrets hit in one wing so its owner can call it a canary or
decoy by design, dead, or live (rotate) WITHOUT reading the drawer, which would print the value into a transcript.

    palace_secrets_classify.py <wing> [<wing> ...]

One tab-separated line per hit (the same strong passes the check runs): table, drawer id, source_file, where
(document, or metadata and its key path), kind, length, shape verdict, heading. Never a value:

- shape: hex-40/64/32 (a git or sha hash, md5), a JWT's alg and whether its exp has passed, a known key prefix and
  what it means (AKIA long-term vs ASIA temporary AWS id, ghp_/gho_/ghs_ GitHub token types, ya29. short-lived
  Google OAuth token, …), a documentation example (AWS's EXAMPLE keys), or base64 with its decoded length and, for
  base64 JSON (the "blob" kind), the decoded object's top-level key NAMES and a "type" from a fixed list;
  otherwise the character classes and the entropy per character;
- heading: the drawer's first heading line (or its first line), itself fully redacted and cut to 100 characters.

Reads the DSN from MEMPALACE_POSTGRES_DSN, else from the daemon env file (PALACE_CHECKS_DSN_FILE).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

TABLES = ("mempalace_drawers", "mempalace_private_drawers")
DOC_EXAMPLES = ("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
PREFIXES = (  # longest first where they nest
    ("github_pat_", "GitHub fine-grained PAT"),
    ("ghp_", "GitHub classic PAT"),
    ("gho_", "GitHub OAuth app token"),
    ("ghu_", "GitHub user-to-server token (8 h)"),
    ("ghs_", "GitHub app installation token (1 h)"),
    ("ghr_", "GitHub refresh token"),
    ("AKIA", "AWS long-term access key id"),
    ("ASIA", "AWS temporary (STS) access key id"),
    ("AIza", "Google API key"),
    ("ya29.", "Google OAuth access token (about 1 h)"),
    ("sk-do-", "DigitalOcean GenAI key"),
    ("sk-ant-", "Anthropic key"),
    ("sk-proj-", "OpenAI project key"),
    ("sk-", "sk- key (OpenAI / LiteLLM style)"),
    ("xoxb-", "Slack bot token"),
    ("xoxp-", "Slack user token"),
    ("ntn_", "Notion integration token"),
    ("secret_", "Notion integration token (old)"),
    ("glpat-", "GitLab PAT"),
)
JSON_TYPES = {"service_account", "authorized_user", "external_account", "JWT", "jwt"}
_HEX = re.compile(r"[0-9a-fA-F]+")
_B64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")


def _entropy(s: str) -> float:
    n = len(s)
    return -sum(c / n * math.log2(c / n) for c in Counter(s).values()) if n else 0.0


def _classes(s: str) -> str:
    parts = [
        name
        for name, ok in (
            ("lower", any(c.islower() for c in s)),
            ("upper", any(c.isupper() for c in s)),
            ("digit", any(c.isdigit() for c in s)),
            ("symbol", any(not c.isalnum() for c in s)),
        )
        if ok
    ]
    return "+".join(parts) or "empty"


def _printable(raw: bytes) -> float:
    return sum(32 <= c < 127 or c in (9, 10, 13) for c in raw) / max(len(raw), 1)


def _b64decode(s: str):
    t = s.rstrip("=")
    for alt in (False, True):
        try:
            raw = t.replace("-", "+").replace("_", "/") if alt else t
            return base64.b64decode(raw + "=" * (-len(raw) % 4), validate=True)
        except (binascii.Error, ValueError):
            continue
    return None


def _json_shape(obj, safe_fn) -> str:
    if not isinstance(obj, dict):
        return f"JSON {type(obj).__name__}"
    keys = ",".join(safe_fn(str(k))[:30] for k in list(obj)[:12])
    more = f",+{len(obj) - 12}" if len(obj) > 12 else ""
    typ = obj.get("type")
    typ_s = f" type={typ}" if isinstance(typ, str) and typ in JSON_TYPES else ""
    exp = obj.get("exp")
    exp_s = ""
    if isinstance(exp, (int, float)):
        when = dt.datetime.fromtimestamp(exp, dt.timezone.utc)
        exp_s = f" exp={when:%Y-%m-%d} ({'expired' if when < dt.datetime.now(dt.timezone.utc) else 'not expired'})"
    return f"JSON object keys [{keys}{more}]{typ_s}{exp_s}"


def shape(value: str, safe_fn=lambda s: s) -> str:
    """A verdict on one matched value that never contains the value. ``safe_fn`` redacts any text taken from it
    (JSON key names) before it is shown."""
    v = value.strip()
    if any(ex in v for ex in DOC_EXAMPLES) or "EXAMPLE" in v:
        return "documentation example (EXAMPLE marker), not a credential"
    if _HEX.fullmatch(v):
        name = {40: "git sha1 / sha1 hash", 64: "sha256 hash", 32: "md5 hash"}.get(len(v))
        return f"hex-{len(v)}" + (f" ({name})" if name else "")
    parts = v.split(".")
    if len(parts) == 3 and v.startswith("eyJ"):
        head = _b64decode(parts[0])
        body = _b64decode(parts[1])
        try:
            h = json.loads(head) if head else {}
            alg = h.get("alg") if isinstance(h, dict) and isinstance(h.get("alg"), str) else "?"
        except ValueError:
            alg = "?"
        try:
            b = json.loads(body) if body else None
        except ValueError:
            b = None
        exp_s = ""
        if isinstance(b, dict) and isinstance(b.get("exp"), (int, float)):
            when = dt.datetime.fromtimestamp(b["exp"], dt.timezone.utc)
            exp_s = f", exp {when:%Y-%m-%d} ({'expired' if when < dt.datetime.now(dt.timezone.utc) else 'not expired'})"
        return f"jwt alg={safe_fn(alg)[:12]}{exp_s}"
    for prefix, name in PREFIXES:
        if v.startswith(prefix):
            return f"prefixed key: {name}"
    if _B64.fullmatch(v) and len(v) >= 16:
        raw = _b64decode(v)
        cut = ""
        if raw is None:  # a run cut off mid-quantum: decode the whole quanta before the cut
            aligned = v.rstrip("=")
            aligned = aligned[: len(aligned) - len(aligned) % 4]
            raw = _b64decode(aligned)
            cut = f", cut at {len(v)} chars (not whole base64)"
            if raw is not None and raw[:1] in (b"{", b"[") and _printable(raw[:4096]) > 0.95:
                try:
                    return f"base64 of {len(raw)}+ bytes{cut}: " + _json_shape(json.loads(raw), safe_fn)
                except ValueError:
                    seen = []
                    for k in re.findall(rb'"([^"\\\x00-\x1f]{1,60})"\s*:', raw[:4096]):
                        name = safe_fn(k.decode())[:30]
                        if name not in seen:
                            seen.append(name)
                    return (
                        f"base64 of {len(raw)}+ bytes{cut}: JSON, does not parse whole; keys seen "
                        f"[{','.join(seen[:12])}]"
                    )
        if raw is not None:
            if raw[:1] in (b"{", b"[") and _printable(raw[:4096]) > 0.95:
                try:
                    return f"base64 of {len(raw)} bytes: " + _json_shape(json.loads(raw), safe_fn)
                except ValueError:
                    return f"base64 of {len(raw)} bytes: JSON-like, does not parse"
            magic = {
                b"\x89PNG": "PNG image",
                b"\x1f\x8b": "gzip",
                b"%PDF": "PDF",
                b"\x30\x82": "DER (certificate or key)",
            }
            for m, name in magic.items():
                if raw.startswith(m):
                    return f"base64 of {len(raw)} bytes: {name}"
            pr = _printable(raw)
            kind = "text" if pr > 0.95 else "binary"
            return f"base64 of {len(raw)} bytes{cut}: {kind} ({pr:.0%} printable)"
    return f"{_classes(v)}, entropy {_entropy(v):.1f} bits/char"


def heading(doc: str, safe_fn) -> str:
    lines = [ln.strip() for ln in (doc or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    first = next((ln for ln in lines if ln.startswith("#")), lines[0])
    return safe_fn(first)[:100].replace("\t", " ")


def _matches(text, passes):
    """(kind, value) for every strong-pass run in ``text`` (the same rule as strong_redact)."""
    out = []
    if not text:
        return out
    for combined, kinds in passes:
        for m in combined.finditer(text):
            if m.group("_mprkeep") is not None:
                continue
            kind = next(k for i, k in enumerate(kinds) if m.group(f"_mpr{i}") is not None)
            out.append((kind, m.group(0)))
    return out


def _walk(value, path=""):
    """(key path, string) for every string in a JSON value, dict keys included."""
    if isinstance(value, str):
        yield path or "$", value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield f"{path}.<key>", k
            yield from _walk(v, f"{path}.{k}" if isinstance(k, str) else path)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def classify_row(table, did, doc, meta, passes, safe_fn, path_fn=None):
    """Lines (tab-separated, value-free) for one drawer's strong-pass hits. ``path_fn`` redacts the source_file
    (default ``safe_fn``); main passes the redactor without its broad shapes so a path stays readable."""
    src = str((meta or {}).get("source_file") or "-") if isinstance(meta, dict) else "-"
    src = (path_fn or safe_fn)(src)
    head = heading(doc, safe_fn)
    lines = []
    sites = [("document", doc)] + [(f"metadata{p}", s) for p, s in _walk(meta)]
    for where, text in sites:
        for kind, value in _matches(text, passes):
            lines.append(
                "\t".join(
                    (table, did, src, safe_fn(where)[:60], kind, str(len(value)), shape(value, safe_fn), head)
                )
            )
    return lines


def _dsn() -> str:
    dsn = os.environ.get("MEMPALACE_POSTGRES_DSN")
    if dsn:
        return dsn
    path = Path(os.path.expanduser(os.environ.get("PALACE_CHECKS_DSN_FILE") or "~/.config/palace-daemon/env"))
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("MEMPALACE_POSTGRES_DSN="):
            return line.split("=", 1)[1].strip().strip('"')
    raise SystemExit("no MEMPALACE_POSTGRES_DSN")


def main(argv=None) -> int:
    wings = list(argv if argv is not None else sys.argv[1:])
    if not wings or any(w.startswith("-") for w in wings):
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 22
    import psycopg

    from mempalace import redact as R

    passes = R.patterns()

    def safe_fn(text: str) -> str:
        return R.redact(text)

    def path_fn(text: str) -> str:
        return R.redact(text, skip_shapes=True)

    print("table\tdrawer_id\tsource_file\twhere\tkind\tlength\tshape\theading")
    n = 0
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute("SET statement_timeout = 600000")
        for table in TABLES:
            for did, doc, meta in conn.execute(
                f"SELECT id, document, metadata FROM {table} WHERE wing = ANY(%s) ORDER BY id", (wings,)
            ):
                for line in classify_row(table, did, doc, meta, passes, safe_fn, path_fn):
                    print(line)
                    n += 1
    print(f"# {n} hit(s) in {','.join(wings)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
