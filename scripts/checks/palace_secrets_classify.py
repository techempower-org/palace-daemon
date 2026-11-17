#!/usr/bin/env python3
"""palace-secrets classify: describe each CHECK palace-secrets hit in one wing so its owner can call it a canary or
decoy by design, dead, or live (rotate) WITHOUT reading the drawer, which would print the value into a transcript.

    palace_secrets_classify.py <wing> [<wing> ...]

One tab-separated line per hit (the same strong passes the check runs): table, drawer id, source, where, kind,
length, shape, heading. Text from a drawer is printed only through an allowlist, never as it is:

- source: "project <dir> session <uuid8>" when the drawer's source_file is a transcript under a project directory
  that exists on this host (~/.claude/projects/<dir>/…), else the source kind only ("other file", "none");
- where: "document", or "metadata.<key>" for a known mempalace metadata key, else "metadata.<other>";
- shape: an ENCODING observation, never a verdict on validity: hex length, a key prefix's documented meaning, an
  exact match of a published documentation example, a JWT's algorithm (from a fixed list) and its UNVERIFIED exp
  claim, base64 with its decoded size and, for base64 JSON (the "blob" kind), its top-level key names from a fixed
  schema list (any other name counts as "<other>"); otherwise the character classes and the entropy per character;
- heading: the drawer's first markdown heading, fully redacted, and only when it holds no credential word and no
  long token-shaped run (else "<withheld>").

Every column is one line: tabs and line breaks are replaced. Reads the DSN from MEMPALACE_POSTGRES_DSN, else from
the daemon env file (PALACE_CHECKS_DSN_FILE).
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

TABLES = ("mempalace_drawers", "mempalace_private_drawers")
PROJECTS = Path(os.path.expanduser("~/.claude/projects"))
# AWS's own published documentation examples; only an EXACT match is named.
DOC_EXAMPLES = {
    "AKIAIOSFODNN7EXAMPLE": "AWS documentation example access key id",
    "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY": "AWS documentation example secret key",
}
PREFIXES = (  # longest first where they nest; meanings are the issuer's documented ones, validity unknown
    ("github_pat_", "GitHub fine-grained PAT"),
    ("ghp_", "GitHub classic PAT"),
    ("gho_", "GitHub OAuth app token"),
    ("ghu_", "GitHub user-to-server token (typically 8 h)"),
    ("ghs_", "GitHub app installation token (typically 1 h)"),
    ("ghr_", "GitHub refresh token"),
    ("AKIA", "AWS long-term access key id"),
    ("ASIA", "AWS temporary (STS) access key id"),
    ("AIza", "Google API key"),
    ("ya29.", "Google OAuth access token (typically 1 h)"),
    ("sk-do-", "DigitalOcean GenAI key"),
    ("sk-ant-", "Anthropic key"),
    ("sk-proj-", "OpenAI project key"),
    ("sk-", "sk- key (OpenAI / LiteLLM style)"),
    ("xoxb-", "Slack bot token"),
    ("xoxp-", "Slack user token"),
    ("ntn_", "Notion integration token"),
    ("secret_", "Notion integration token (old format)"),
    ("glpat-", "GitLab PAT"),
)
# JSON key names that may be shown: schema names of the credential and token formats a "blob" usually is.
SCHEMA_KEYS = frozenset(
    """type project_id private_key_id private_key client_email client_id client_secret auth_uri token_uri
    auth_provider_x509_cert_url client_x509_cert_url universe_domain refresh_token access_token id_token token_type
    expires_in expires_at expiry scope scopes alg enc kid cty jti typ iss sub aud exp iat nbf azp nonce
    at_least auths auth username password email serveraddress identitytoken registrytoken kind apiVersion
    metadata data name namespace user users clusters contexts current-context tiles version account_id
    tenant_id subscription_id key secret token session cookies headers url host port database""".split()
)
JSON_TYPES = frozenset({"service_account", "authorized_user", "external_account"})
ALGS = frozenset(
    """HS256 HS384 HS512 RS256 RS384 RS512 ES256 ES384 ES512 PS256 PS384 PS512 EdDSA none dir RSA-OAEP
    RSA-OAEP-256 A128KW A192KW A256KW A128GCMKW A256GCMKW ECDH-ES ECDH-ES+A128KW ECDH-ES+A256KW""".split()
)
META_KEYS = frozenset(
    """source_file source_path chunk_index chunk_total wing room topic entities filed_at authored_at content_hash
    extract_mode ingest_mode normalize_version id_recipe source_mtime source_dir_ino novelty_tag hall
    convo_chunker_version aaak_source agent source_kind""".split()
)
CRED_WORDS = re.compile(r"(?i)pass(?:word|wd|phrase)?|secret|token|api.?key|private|bearer|credential|auth")
TOKEN_RUN = re.compile(r"[A-Za-z0-9+/=_.-]{16,}")
_HEX = re.compile(r"[0-9a-fA-F]+")
_B64 = re.compile(r"[A-Za-z0-9+/_-]+={0,2}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_MAX_TS = 253402300799  # 9999-12-31


def one_line(text: str) -> str:
    return re.sub(r"[\t\r\n\x00-\x1f\x7f  \x85]", " ", str(text))


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


def _exp(value) -> str:
    """An UNVERIFIED exp claim, described; never raises."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return "invalid exp claim"
    if not 0 <= value <= _MAX_TS:
        return "invalid exp claim"
    try:
        when = dt.datetime.fromtimestamp(value, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return "invalid exp claim"
    past = when < dt.datetime.now(dt.timezone.utc)
    return f"unverified exp claim {when:%Y-%m-%d} ({'past' if past else 'future'})"


def _names(keys) -> str:
    keys = list(keys)
    shown = [k if isinstance(k, str) and k in SCHEMA_KEYS else "<other>" for k in keys[:12]]
    more = f",+{len(keys) - 12}" if len(keys) > 12 else ""
    return ",".join(shown) + more


def _json_shape(obj) -> str:
    if not isinstance(obj, dict):
        return f"JSON {type(obj).__name__}"
    typ = obj.get("type")
    typ_s = f" type={typ}" if isinstance(typ, str) and typ in JSON_TYPES else ""
    exp_s = f"; {_exp(obj['exp'])}" if "exp" in obj else ""
    return f"JSON object keys [{_names(obj)}]{typ_s}{exp_s}"


def _loads(raw: bytes):
    return json.loads(raw, parse_constant=lambda _c: None)  # NaN / Infinity: no value


def shape(value: str) -> str:
    """An encoding observation on one matched value. It never contains the value or text taken from it, except
    names from the fixed lists above. It is never a verdict that the value is valid or harmless."""
    v = value.strip()
    if v in DOC_EXAMPLES:
        return f"exact match: {DOC_EXAMPLES[v]}"
    if _HEX.fullmatch(v):
        return f"hex-{len(v)} (hash-like format; validity unknown)"
    parts = v.split(".")
    if len(parts) == 3 and v.startswith("eyJ"):
        alg, exp_s = "<other>", ""
        try:
            h = _loads(_b64decode(parts[0]) or b"")
            if isinstance(h, dict) and h.get("alg") in ALGS:
                alg = h["alg"]
        except ValueError:
            pass
        try:
            b = _loads(_b64decode(parts[1]) or b"")
            if isinstance(b, dict) and "exp" in b:
                exp_s = f", {_exp(b['exp'])}"
        except ValueError:
            pass
        return f"jwt alg={alg}{exp_s}"
    for prefix, name in PREFIXES:
        if v.startswith(prefix):
            return f"prefixed key: {name}; validity unknown"
    if _B64.fullmatch(v) and len(v) >= 16:
        raw = _b64decode(v)
        cut = ""
        if raw is None:  # a run cut off mid-quantum: decode the whole quanta before the cut
            aligned = v.rstrip("=")
            raw = _b64decode(aligned[: len(aligned) - len(aligned) % 4])
            cut = f", cut at {len(v)} chars (not whole base64)"
        if raw is not None:
            size = f"base64 of {len(raw)}{'+' if cut else ''} bytes{cut}"
            if raw[:1] in (b"{", b"[") and _printable(raw[:4096]) > 0.95:
                try:
                    return f"{size}: " + _json_shape(_loads(raw))
                except ValueError:
                    seen = []
                    for k in re.findall(rb'"([^"\\\x00-\x1f]{1,60})"\s*:', raw[:4096]):
                        if k.decode("utf-8", "replace") not in seen:
                            seen.append(k.decode("utf-8", "replace"))
                    return f"{size}: JSON, does not parse whole; keys seen [{_names(seen)}]"
            magic = {b"\x89PNG": "PNG image", b"\x1f\x8b": "gzip", b"%PDF": "PDF", b"\x30\x82": "DER"}
            for m, name in magic.items():
                if raw.startswith(m):
                    return f"{size}: {name} format (may still hold a credential)"
            pr = _printable(raw)
            return f"{size}: {'text' if pr > 0.95 else 'binary'} ({pr:.0%} printable)"
    return f"{_classes(v)}, entropy {_entropy(v):.1f} bits/char"


def source(meta, projects=PROJECTS) -> str:
    sf = meta.get("source_file") if isinstance(meta, dict) else None
    if not isinstance(sf, str) or not sf:
        return "none"
    root = str(projects).rstrip("/") + "/"
    if sf.startswith(root):
        rest = sf[len(root):].split("/")
        if rest and rest[0] and (projects / rest[0]).is_dir() and rest[0] in os.listdir(projects):
            u = next((m.group(0) for p in rest[1:] for m in [_UUID.match(p)] if m), None)
            return f"project {rest[0]}" + (f" session {u[:8]}" if u else "")
    return "other file"


def where_label(path: str) -> str:
    if path == "document":
        return "document"
    head = path.split(".", 2)[1] if path.startswith("metadata.") else ""
    head = head.split("[", 1)[0]
    return f"metadata.{head}" if head in META_KEYS else "metadata.<other>"


def heading(doc: str, safe_fn) -> str:
    first = next((ln.strip() for ln in (doc or "").splitlines() if ln.strip().startswith("#")), "")
    if not first:
        return ""
    if CRED_WORDS.search(first) or TOKEN_RUN.search(first):
        return "<withheld>"
    clean = safe_fn(first)
    if clean != first:
        return "<withheld>"
    return one_line(clean)[:80]


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


def _walk(value, path="metadata"):
    """(key path, string) for every string in a JSON value, dict keys included."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                yield f"{path}.<key>", k
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            yield from _walk(v, f"{path}[{i}]")


def classify_row(table, did, doc, meta, passes, safe_fn, projects=PROJECTS):
    """Lines (tab-separated, one line each, value-free) for one drawer's strong-pass hits."""
    src = source(meta, projects)
    head = heading(doc, safe_fn)
    lines = []
    sites = [("document", doc)] + list(_walk(meta))
    for where, text in sites:
        for kind, value in _matches(text, passes):
            cols = (table, did, src, where_label(where), kind, str(len(value)), shape(value), head)
            lines.append("\t".join(one_line(c) for c in cols))
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
        print("usage: palace_secrets_classify.py <wing> [<wing> ...]", file=sys.stderr)
        return 22
    import psycopg

    from mempalace import redact as R

    passes = R.patterns()
    print("table\tdrawer_id\tsource\twhere\tkind\tlength\tshape\theading")
    n = 0
    with psycopg.connect(_dsn(), autocommit=True) as conn:
        conn.execute("SET statement_timeout = 600000")
        for table in TABLES:
            for did, doc, meta in conn.execute(
                f"SELECT id, document, metadata FROM {table} WHERE wing = ANY(%s) ORDER BY id", (wings,)
            ):
                for line in classify_row(table, did, doc, meta, passes, R.redact):
                    print(line)
                    n += 1
    print(f"# {n} hit(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
