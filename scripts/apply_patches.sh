#!/usr/bin/env bash
# apply_patches.sh — re-apply local patches to the mempalace pipx install
# Run this after every: pipx upgrade mempalace
#
# Usage:
#   bash scripts/apply_patches.sh
#   bash scripts/apply_patches.sh --check   # dry-run, no changes

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCHES_DIR="$SCRIPT_DIR/../patches"

# Locate the python interpreter of the mempalace install. Try in order:
#   1. $MEMPALACE_PYTHON override (escape hatch for non-standard layouts)
#   2. pipx venv: $(pipx environment --value PIPX_LOCAL_VENVS)/mempalace/bin/python
#   3. the `mempalace` binary on PATH — derive its venv via ../bin/python
# Patches target the installed package's source tree, so we need *that*
# venv's site-packages, not whatever interpreter happens to run this script.
find_mempalace_python() {
    if [[ -n "${MEMPALACE_PYTHON:-}" && -x "$MEMPALACE_PYTHON" ]]; then
        echo "$MEMPALACE_PYTHON"
        return 0
    fi
    if command -v pipx >/dev/null 2>&1; then
        local pipx_venvs
        pipx_venvs="$(pipx environment --value PIPX_LOCAL_VENVS 2>/dev/null || true)"
        if [[ -n "$pipx_venvs" && -x "$pipx_venvs/mempalace/bin/python" ]]; then
            echo "$pipx_venvs/mempalace/bin/python"
            return 0
        fi
    fi
    local mempalace_bin
    mempalace_bin="$(command -v mempalace 2>/dev/null || true)"
    if [[ -n "$mempalace_bin" ]]; then
        local resolved venv_python
        resolved="$(readlink -f "$mempalace_bin")"
        venv_python="$(dirname "$resolved")/python"
        if [[ -x "$venv_python" ]]; then
            echo "$venv_python"
            return 0
        fi
    fi
    return 1
}

if ! MEMPALACE_PY="$(find_mempalace_python)"; then
    echo "error: could not locate the mempalace install's python interpreter." >&2
    echo "  tried: \$MEMPALACE_PYTHON, pipx venvs, and 'mempalace' on PATH." >&2
    echo "  set MEMPALACE_PYTHON=/path/to/venv/bin/python and re-run." >&2
    exit 1
fi

VENV_SITE="$("$MEMPALACE_PY" -c 'import site; print(site.getsitepackages()[0])')"

DRY_RUN=0
[[ "${1:-}" == "--check" ]] && DRY_RUN=1

MEMPALACE_VERSION="$("$MEMPALACE_PY" \
    -c 'import mempalace; print(mempalace.__version__)' 2>/dev/null || echo unknown)"

echo "mempalace version : $MEMPALACE_VERSION"
echo "site-packages     : $VENV_SITE"
echo "patches dir       : $PATCHES_DIR"
[[ $DRY_RUN -eq 1 ]] && echo "(dry-run — no changes will be made)"
echo ""

APPLIED=0
SKIPPED=0
FAILED=0

for patch in "$PATCHES_DIR"/*.patch; do
    [[ -f "$patch" ]] || continue
    name="$(basename "$patch")"

    # Check if already applied
    if patch --dry-run -p1 -R --quiet -d "$VENV_SITE" < "$patch" 2>/dev/null; then
        echo "  [already applied] $name"
        ((SKIPPED++)) || true
        continue
    fi

    # Check if applicable
    if ! patch --dry-run -p1 --quiet -d "$VENV_SITE" < "$patch" 2>/dev/null; then
        echo "  [CONFLICT]        $name  <-- upstream may have changed this code; review manually"
        ((FAILED++)) || true
        continue
    fi

    if [[ $DRY_RUN -eq 1 ]]; then
        echo "  [would apply]     $name"
        ((APPLIED++)) || true
    else
        patch -p1 -d "$VENV_SITE" < "$patch"
        echo "  [applied]         $name"
        ((APPLIED++)) || true
    fi
done

echo ""
echo "Results: $APPLIED applied, $SKIPPED already-applied, $FAILED conflicts"

if [[ $FAILED -gt 0 ]]; then
    echo ""
    echo "Action required: $FAILED patch(es) conflicted."
    echo "Check if upstream fixed the issue — if so, remove the patch file."
    echo "Otherwise update the patch to match the new upstream code."
    exit 1
fi

if [[ $DRY_RUN -eq 0 && $APPLIED -gt 0 ]]; then
    echo ""
    echo "Restart the daemon to pick up changes:"
    echo "  sudo systemctl restart palace-daemon"
fi
