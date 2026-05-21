#!/usr/bin/env bash
# mempalace-mcp-wrapper — env-aware launcher for the stdio MCP bridge.
#
# What it does:
#   1. Sources ~/.config/palace-daemon/env (PALACE_API_KEY, PALACE_DAEMON_URL).
#   2. Locates the bridge (clients/mempalace-mcp.py, sibling of this script).
#   3. exec's the bridge so the MCP client (opencode, Claude Code, etc.)
#      sees the bridge's stdio directly — this script adds zero overhead
#      to the long-lived stdio session.
#
# Why this exists:
#   MCP clients spawn server processes WITHOUT inheriting the parent shell's
#   rc files. The daemon's API key normally lives in ~/.config/palace-daemon/env
#   (mode 600), which the bridge reads from os.environ. Without this wrapper,
#   the only options are:
#     a) export PALACE_API_KEY in shell rc (leaks the key into every subprocess)
#     b) plant the key in the client's config file (plaintext on disk)
#   Both are worse than a 30-line shell wrapper.
#
# Usage in client configs:
#   # opencode (~/.config/opencode/opencode.jsonc)
#   "mcp": {
#     "mempalace": {
#       "type": "local",
#       "command": ["/home/<user>/Projects/palace-daemon/clients/mempalace-mcp-wrapper.sh"],
#       "enabled": true
#     }
#   }
#
#   # Claude Code (~/.claude.json or plugin .mcp.json)
#   "mempalace": {
#     "command": "/home/<user>/Projects/palace-daemon/clients/mempalace-mcp-wrapper.sh"
#   }
#
# Environment overrides (all optional):
#   PALACE_DAEMON_ENV     env file to source (default: ~/.config/palace-daemon/env)
#   PALACE_DAEMON_URL     daemon URL (default: read from env file, then http://localhost:8085)
#   PALACE_DAEMON_BRIDGE  path to mempalace-mcp.py (default: sibling of this script)
#   PALACE_DAEMON_PYTHON  python interpreter (default: python3 on PATH)
#
set -euo pipefail

# Resolve our own location so the bridge can default to a sibling.
_self="$(readlink -f "${BASH_SOURCE[0]}" 2>/dev/null || realpath "${BASH_SOURCE[0]}" 2>/dev/null || echo "${BASH_SOURCE[0]}")"
_dir="$(cd "$(dirname "$_self")" && pwd)"

ENV_FILE="${PALACE_DAEMON_ENV:-$HOME/.config/palace-daemon/env}"
BRIDGE="${PALACE_DAEMON_BRIDGE:-$_dir/mempalace-mcp.py}"
PYTHON="${PALACE_DAEMON_PYTHON:-python3}"

# Source the env file if present. `set -a` exports every var assigned inside.
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
fi

# After sourcing, fall back to a sane localhost default if the env file
# didn't set PALACE_DAEMON_URL.
DAEMON="${PALACE_DAEMON_URL:-http://localhost:8085}"

if [[ ! -f "$BRIDGE" ]]; then
    echo "mempalace-mcp-wrapper: bridge script not found at $BRIDGE" >&2
    echo "  Set PALACE_DAEMON_BRIDGE to override." >&2
    exit 2
fi

exec "$PYTHON" "$BRIDGE" --daemon "$DAEMON" "$@"
