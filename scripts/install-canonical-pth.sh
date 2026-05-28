#!/usr/bin/env bash
# install-canonical-pth.sh — ensure palace-daemon's source dir is on the
# daemon venv's sys.path via a `.pth` file (palace-daemon issue #79).
#
# Why a .pth and not PYTHONPATH:
#   mempalace's __init__.py (`_strip_leaked_pythonpath_from_sys_path`)
#   removes sys.path entries that came from PYTHONPATH — defensive ABI
#   hygiene against multi-Python compiled-extension contamination. That
#   strip is correct in spirit but silently breaks `from kg_canonical_writepass
#   import ...` in the kg-extract worker (mempalace #279). `.pth`-installed
#   entries land in sys.path via site-init, not PYTHONPATH, so the strip
#   leaves them alone. Becomes redundant when techempower-org/mempalace#281
#   ports the modules INTO the mempalace package.
#
# Without this file, a venv rebuild (`pip install -r requirements.txt` against
# a fresh venv) silently re-breaks canonical mapping — kg-extract falls
# through to its identity-fallback `map_for_write` and writes raw predicates
# again. We learned this on 2026-05-27 the hard way.
#
# Idempotent — exits 0 with no change if the .pth is already correct.
#
# Configuration (env var → default):
#   PALACE_VENV     daemon venv path     (/home/jp/.local/share/palace-daemon/venv)
#   PALACE_SOURCE   palace-daemon source (/home/jp/Projects/palace-daemon)
set -euo pipefail

VENV="${PALACE_VENV:-/home/jp/.local/share/palace-daemon/venv}"
SOURCE="${PALACE_SOURCE:-/home/jp/Projects/palace-daemon}"

if [ ! -d "$VENV/lib" ]; then
    echo "install-canonical-pth: no venv at $VENV (skipping)" >&2
    exit 0   # don't break boot if the venv isn't where we expect
fi
if [ ! -d "$SOURCE" ]; then
    echo "install-canonical-pth: no source at $SOURCE (skipping)" >&2
    exit 0
fi

# Find the venv's site-packages (python3.X subdir varies).
site_packages=""
for p in "$VENV"/lib/python*/site-packages; do
    [ -d "$p" ] && { site_packages="$p"; break; }
done
if [ -z "$site_packages" ]; then
    echo "install-canonical-pth: no site-packages under $VENV/lib (skipping)" >&2
    exit 0
fi

pth="$site_packages/palace-daemon-source.pth"
if [ -f "$pth" ] && grep -qxF "$SOURCE" "$pth" 2>/dev/null; then
    echo "install-canonical-pth: ✓ $pth already correct"
    exit 0
fi

# Write atomically — never leave a half-formed .pth that confuses site-init.
tmp="$(mktemp "${pth}.XXXXXX")"
echo "$SOURCE" > "$tmp"
mv -f "$tmp" "$pth"
echo "install-canonical-pth: ✓ wrote $pth → $SOURCE"
