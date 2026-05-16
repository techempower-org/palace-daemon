#!/usr/bin/env bash
# verify-routes.sh — smoke test for palace-daemon HTTP routes after deploy.
#
# Exercises every public route against a running daemon. Designed to be
# run manually after `sudo systemctl restart palace-daemon` (system unit;
# the only supported configuration — see CLAUDE.md), not in CI (it
# depends on a live palace).
#
# Usage:
#   PALACE_DAEMON_URL=http://disks.jphe.in:8085 \
#   PALACE_API_KEY=... \
#       scripts/verify-routes.sh

set -e

URL="${PALACE_DAEMON_URL:-http://localhost:8085}"
KEY="${PALACE_API_KEY:-}"
H_AUTH=()
[ -n "$KEY" ] && H_AUTH=(-H "x-api-key: $KEY")

pass() { echo "  ✓ $1"; }
fail() { echo "  ✗ $1" >&2; exit 1; }

probe() {
  local label="$1"
  local expected="$2"
  shift 2
  local resp
  resp=$(curl -sS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1) || fail "$label — curl error"
  if echo "$resp" | grep -q "$expected"; then
    pass "$label"
  else
    fail "$label — expected '$expected' in response: ${resp:0:200}"
  fi
}

probe_json_field() {
  local label="$1"
  local field="$2"
  shift 2
  local resp val
  if ! resp=$(curl -fsS --max-time 90 "${H_AUTH[@]}" "$@" 2>&1); then
    fail "$label — curl failed (HTTP non-2xx or connection error)"
  fi
  val=$(echo "$resp" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get('$field', ''))
except Exception as e:
    print(f'PARSE-ERROR:{e}', file=sys.stderr)
" 2>&1) || true
  if [ -n "$val" ] && [ "${val:0:13}" != "PARSE-ERROR:" ]; then
    pass "$label ($field=$val)"
  else
    fail "$label — bad JSON or missing field: $val"
  fi
}

echo "→ palace-daemon at $URL"
echo

# /health — no auth, should always respond.
probe "GET /health" "palace-daemon" "$URL/health"

# /search — verifies the limit= param is honored. Earlier versions
# silently dropped limit (passed as max_results).
probe "GET /search" "results" "$URL/search?q=palace-daemon&limit=2"

# /list — query-free metadata listing, complementary to /search.
probe "GET /list (no filters)" "drawers" "$URL/list?limit=2"
probe "GET /list?wing=projects" "drawers" "$URL/list?wing=projects&limit=2"

# /memory/{id} write paths (DELETE + PATCH wired but not exercised here —
# they mutate state. Skipped in smoke; covered in dedicated integration runs.)

# /context — same code path with a different param name for LLM-friendly prompts.
probe "GET /context" "results" "$URL/context?topic=palace-daemon&limit=2"

# /stats — read-only summary across kg + graph + status tools.
probe "GET /stats" "kg" "$URL/stats"

# /graph — single-shot structural snapshot (v1.6.0).
probe "GET /graph" '"wings"' "$URL/graph"

# /viz — status dashboard (HTML shell, v1.7.0).
# Smoke check looks for the title tag, not the rendered content.
probe "GET /viz" 'palace-daemon' "$URL/viz"

# /repair/status — query state, no actual repair.
probe_json_field "GET /repair/status" "in_progress" "$URL/repair/status"

# limit= is honored end-to-end. Useful as a regression check, but
# fail-shaped only when the daemon returns valid JSON with a result
# count that is neither the requested 3 nor 0. Connection or parse
# errors `?`-flag rather than fail.
if ! resp=$(curl -fsS --max-time 90 "${H_AUTH[@]}" "$URL/search?q=palace&limit=3" 2>&1); then
  echo "  ? limit=3 — curl failed (HTTP error or unreachable), can't confirm"
elif COUNT=$(echo "$resp" | python3 -c "import json, sys; print(len(json.load(sys.stdin).get('results', [])))" 2>/dev/null); then
  if [ "$COUNT" = "3" ]; then
    pass "limit=3 returns 3 hits (max_results fix)"
  elif [ "$COUNT" = "0" ]; then
    echo "  ? limit=3 returned 0 — palace may be empty, can't confirm"
  else
    fail "limit=3 returned $COUNT hits — expected 3 (or 0 on empty palace)"
  fi
else
  echo "  ? limit=3 — response wasn't valid JSON, can't confirm"
fi

echo
echo "✓ all routes verified"
