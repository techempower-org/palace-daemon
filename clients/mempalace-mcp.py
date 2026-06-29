#!/usr/bin/env python3
"""
mempalace-mcp — stdio MCP proxy for palace-daemon

Primary mode: bridges MCP client → palace-daemon over HTTP (serialized,
semaphore-protected, all clients coordinated through one chokepoint).

Safety mode: if the daemon is unreachable at startup, the client exits
with an error. Direct database access is disabled to prevent "split-brain"
concurrency issues and SQLite corruption.

cli-only mode (mcp_mode=cli-only) serves the entire MCP surface locally —
handshake answered in-process, zero tools advertised, calls rejected — and
never contacts the daemon, so an asleep palace host doesn't surface as a
"Failed to connect" MCP error in every client session.

search-only mode (mcp_mode=search-only) is the middle ground: it advertises
exactly one tool (mempalace_search, ~300 tokens of schema vs ~9k for the full
surface). Handshake and tools/list are answered locally; only an actual search
call reaches the daemon, and if the host is asleep it sends the configured
auto_wake command and retries once before falling back to a friendly message.

Usage:
    python mempalace-mcp.py --daemon http://localhost:8085
    PALACE_DAEMON_URL=http://localhost:8085 python mempalace-mcp.py

Claude Code setup (~/.claude.json mcpServers):
    {
      "mempalace": {
        "type": "stdio",
        "command": "python3",
        "args": ["/path/to/mempalace-mcp.py", "--daemon", "http://localhost:8085"]
      }
    }
"""
import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_DAEMON = os.getenv("PALACE_DAEMON_URL", "http://localhost:8085")
API_KEY = os.getenv("PALACE_API_KEY", "")

CONFIG_PATH = os.path.expanduser(
    os.getenv("MEMPALACE_CONFIG", "~/.mempalace/config.json")
)
# Valid mcp_mode values. Anything else (unset, typo, garbled) falls open to "all".
#   all         — full daemon-backed tool surface (~39 tools, ~9k tokens)
#   cli-only    — zero tools advertised, never contacts the daemon
#   search-only — exactly one tool (mempalace_search, ~300 tokens), forwarded
#                 to the daemon with auto-wake on a sleeping host
VALID_MCP_MODES = ("all", "cli-only", "search-only")

CLI_ONLY_REJECT_MESSAGE = (
    "MCP tools are disabled (mcp_mode=cli-only). Use the mempalace CLI, "
    "or set mcp_mode=all and reconnect."
)

SEARCH_ONLY_REJECT_MESSAGE = (
    "Only mempalace_search is available in search-only mode. "
    "Use the mempalace CLI for other operations."
)

# Hardcoded single-tool surface for search-only mode. Deliberately slimmed vs
# the full daemon-side mempalace_search schema (which exposes ~10 parameters
# incl. candidate_strategy, fusion_mode, include_trace, …): only query, limit,
# and wing — the three that matter for conversational recall. Advanced
# parameters remain available via the CLI. This keeps the per-turn schema cost
# at ~300 tokens instead of ~800.
SEARCH_ONLY_TOOLS = [
    {
        "name": "mempalace_search",
        "description": (
            "Search your memory palace. Returns verbatim drawer content "
            "with similarity scores. Use short keyword queries (not full "
            "sentences). 410K+ drawers across 100+ wings."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Short search query — keywords or a question. Max 250 chars.",
                    "maxLength": 250,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 5, max 20)",
                    "minimum": 1,
                    "maximum": 20,
                    "default": 5,
                },
                "wing": {
                    "type": "string",
                    "description": "Filter by wing (optional). Use for project-scoped searches.",
                },
            },
            "required": ["query"],
        },
    }
]


def resolve_mcp_mode() -> str:
    """Resolve the effective mcp_mode.

    Precedence: PALACE_MCP_MODE env override > config file > default "all".
    Fail-open: any unknown value, missing/unreadable/garbled config, or
    missing key resolves to "all" so a typo never silently kills the tool
    surface. Only an explicit "cli-only" suppresses tools.
    """
    env = os.getenv("PALACE_MCP_MODE")
    if env is not None:
        return env if env in VALID_MCP_MODES else "all"
    try:
        with open(CONFIG_PATH) as fh:
            mode = json.load(fh).get("mcp_mode", "all")
    except Exception:
        return "all"
    return mode if mode in VALID_MCP_MODES else "all"


def find_daemon(url: str) -> bool:
    try:
        req = urllib.request.urlopen(url.rstrip("/") + "/health", timeout=3)
        return req.status == 200
    except Exception:
        return False


def forward(url: str, request: dict) -> dict:
    data = json.dumps(request).encode()
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-Api-Key"] = API_KEY
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp",
        data=data,
        headers=headers,
        method="POST",
    )
    # 120s headroom for slow read tools (e.g. mempalace_status walking a
    # multi-GB palace) plus short waits behind in-flight writes on the
    # daemon's read semaphore. Override with PALACE_MCP_TIMEOUT for tuning.
    raw_timeout = os.getenv("PALACE_MCP_TIMEOUT", "120")
    try:
        timeout = int(raw_timeout)
    except ValueError:
        print(
            f"warning: PALACE_MCP_TIMEOUT='{raw_timeout}' is not an integer; "
            "falling back to 120s",
            file=sys.stderr,
        )
        timeout = 120
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _load_auto_wake_command() -> str:
    """Read auto_wake.command from the mempalace config (stdlib-only).

    Returns the command string (e.g. "realm wol wake familiar"), or "" if the
    key is unset, the config is missing/unreadable, or the value isn't a
    string. No mempalace imports — this must work even when the package isn't
    importable. Same config the CLI uses (~/.mempalace/config.json).
    """
    try:
        with open(CONFIG_PATH) as fh:
            cfg = json.load(fh)
    except Exception:
        return ""
    auto_wake = cfg.get("auto_wake")
    if not isinstance(auto_wake, dict):
        return ""
    command = auto_wake.get("command")
    return command if isinstance(command, str) else ""


def _forward_with_autowake(daemon_url: str, request: dict) -> dict:
    """Forward to the daemon; on a connection failure, try one auto-wake cycle.

    The palace host is Slumber-Ward sleepable, so a URLError usually means the
    host is asleep rather than truly down. If auto_wake is configured, send the
    WoL command and retry once. Either way, never raise — fall back to a usable
    'host is waking' tool result so the model gets text instead of a hard tool
    error. HTTPError is handled separately: a 4xx/5xx means the daemon is awake
    and rejecting, so waking it wouldn't help (mirrors forward()'s split in the
    'all' path — see palace-daemon#7).
    """
    rid = request.get("id")
    try:
        return forward(daemon_url, request)
    except urllib.error.HTTPError as e:
        return {"jsonrpc": "2.0", "id": rid,
                "error": {"code": -32000,
                          "message": f"Daemon rejected request (HTTP {e.code} {e.reason})"}}
    except urllib.error.URLError:
        wake_cmd = _load_auto_wake_command()
        if wake_cmd:
            try:
                # shlex.split (no shell=True) per security review — the command
                # comes from a local config file, but parsing it as argv avoids
                # shell injection if that file is ever attacker-influenced.
                subprocess.run(shlex.split(wake_cmd), timeout=15,
                               capture_output=True)
                time.sleep(3)
                return forward(daemon_url, request)
            except Exception:
                # Wake failed, command malformed, or host still not up after
                # 3s (it takes ~20s) — fall through to the graceful message.
                pass
        return {"jsonrpc": "2.0", "id": rid,
                "result": {
                    "content": [{"type": "text", "text":
                        "Palace host is waking up (WoL sent). "
                        "Try again in ~20 seconds, or use `mempalace search` CLI."}],
                    "isError": False,
                }}


def _local_handshake_response(method: str, request: dict, mode_label: str):
    """Local JSON-RPC responses for handshake methods that never need the daemon.

    Shared by the cli-only and search-only branches. Returns a response dict for
    initialize / ping / resources/list / prompts/list, or None if `method` is
    none of those (the caller then handles tools/* and the not-found fallback
    per its mode). `mode_label` is reported as serverInfo.version.
    """
    rid = request.get("id")
    if method == "initialize":
        params = request.get("params") or {}
        protocol = params.get("protocolVersion")
        if not isinstance(protocol, str):
            protocol = "2025-11-25"
        return {"jsonrpc": "2.0", "id": rid,
                "result": {"protocolVersion": protocol,
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "mempalace",
                                          "version": mode_label}}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    if method == "resources/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"resources": []}}
    if method == "prompts/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"prompts": []}}
    return None


def _stdio_loop(handle_line):
    """Read JSON-RPC lines from stdin, call handle_line, print responses."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Only single JSON-RPC objects are supported. A batch (list) or a
        # bare scalar/null would make request.get(...) raise AttributeError
        # and crash the loop — skip them like a parse error.
        if not isinstance(request, dict):
            continue
        response = handle_line(request)
        if response is not None and request.get("id") is not None:
            print(json.dumps(response), flush=True)


def run_daemon_mode(daemon_url: str, mcp_mode: str = "all"):
    def handle(request):
        # Defensive: a non-dict request (batch list, scalar, null) has no
        # .get — skip it rather than crash. _stdio_loop already filters
        # these, but guard here too so handle() is safe in isolation.
        if not isinstance(request, dict):
            return None
        if mcp_mode == "cli-only":
            method = request.get("method")
            # Notifications carry no id and expect no response.
            if request.get("id") is None:
                return None
            # The whole surface is served locally: the daemon may be asleep
            # (Slumber Ward S3) and cli-only must never depend on it.
            handshake = _local_handshake_response(method, request, "cli-only")
            if handshake is not None:
                return handshake
            # tools/list → advertise zero tools, so the MCP client caches an
            # empty surface (reclaiming ~9k tokens of schema context).
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"tools": []}}
            # tools/call → reject; nothing was advertised, so nothing runs.
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32601,
                                  "message": CLI_ONLY_REJECT_MESSAGE}}
            # Anything else: method-not-found, never forwarded.
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32601,
                              "message": f"Method not available in "
                                         f"cli-only mode: {method}"}}
        if mcp_mode == "search-only":
            method = request.get("method")
            # Notifications carry no id and expect no response.
            if request.get("id") is None:
                return None
            # Handshake + resources/prompts answered locally — only an actual
            # mempalace_search call reaches out to the (possibly asleep) daemon.
            handshake = _local_handshake_response(method, request, "search-only")
            if handshake is not None:
                return handshake
            # tools/list → exactly one tool, served locally (no daemon contact).
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"tools": SEARCH_ONLY_TOOLS}}
            # tools/call → only mempalace_search is allowed; forward it (with
            # auto-wake on a sleeping host). Everything else → -32601.
            if method == "tools/call":
                params = request.get("params") or {}
                if params.get("name") == "mempalace_search":
                    return _forward_with_autowake(daemon_url, request)
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32601,
                                  "message": SEARCH_ONLY_REJECT_MESSAGE}}
            # Anything else: method-not-found, never forwarded.
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32601,
                              "message": f"Method not available in "
                                         f"search-only mode: {method}"}}
        try:
            return forward(daemon_url, request)
        except urllib.error.HTTPError as e:
            # 4xx/5xx from the daemon — auth failures, missing endpoints,
            # etc. HTTPError is a subclass of URLError, so split it BEFORE
            # the generic URLError handler. Otherwise a 401 silently
            # surfaces as "Daemon unreachable" and the operator goes
            # hunting for network gremlins. See palace-daemon#7.
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32000,
                              "message": f"Daemon rejected request (HTTP {e.code} {e.reason})"}}
        except urllib.error.URLError as e:
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32000, "message": f"Daemon unreachable: {e}"}}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": request.get("id"),
                    "error": {"code": -32000, "message": str(e)}}

    _stdio_loop(handle)


def main():
    parser = argparse.ArgumentParser(description="MCP stdio proxy for palace-daemon")
    parser.add_argument("--daemon", default=DEFAULT_DAEMON, help="palace-daemon base URL")
    parser.add_argument("--api-key", default=None, help="API key (or set PALACE_API_KEY)")
    args = parser.parse_args()

    global API_KEY
    if args.api_key is not None:
        API_KEY = args.api_key

    mcp_mode = resolve_mcp_mode()
    if mcp_mode == "cli-only":
        # cli-only never contacts the daemon, so skip the startup probe
        # entirely — an asleep palace host would otherwise stall every
        # client session for the probe's 3s timeout before serving locally.
        print("palace-daemon: cli-only mode - serving MCP handshake locally "
              "(daemon probe skipped)", file=sys.stderr)
        run_daemon_mode(args.daemon, mcp_mode)
    elif mcp_mode == "search-only":
        # search-only answers the handshake + tools/list locally and only
        # contacts the daemon when mempalace_search is actually called (with
        # auto-wake on a sleeping host). Probe at startup for an accurate log
        # line, but NEVER exit on failure — the single tool stays usable and
        # search auto-wakes on demand.
        if find_daemon(args.daemon):
            print(f"palace-daemon: search-only mode - connected at {args.daemon} "
                  "(1 tool: mempalace_search)", file=sys.stderr)
        else:
            print(f"palace-daemon: search-only mode - daemon unreachable at "
                  f"{args.daemon}; serving tools/list locally, search will "
                  "auto-wake on demand", file=sys.stderr)
        run_daemon_mode(args.daemon, mcp_mode)
    elif find_daemon(args.daemon):
        # Log connection success to stderr so it doesn't break JSON-RPC stdout
        print(f"palace-daemon: connected at {args.daemon} (mcp_mode={mcp_mode})", file=sys.stderr)
        run_daemon_mode(args.daemon, mcp_mode)
    else:
        print(f"ERROR: palace-daemon unreachable at {args.daemon}. Direct fallback disabled for safety.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
