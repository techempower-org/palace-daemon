#!/usr/bin/env python3
"""
mempalace-mcp — stdio MCP proxy for palace-daemon

Primary mode: bridges MCP client → palace-daemon over HTTP (serialized,
semaphore-protected, all clients coordinated through one chokepoint).

Safety mode: if the daemon is unreachable at startup, the client exits
with an error. Direct database access is disabled to prevent "split-brain"
concurrency issues and SQLite corruption.

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
import sys
import urllib.error
import urllib.request

DEFAULT_DAEMON = os.getenv("PALACE_DAEMON_URL", "http://localhost:8085")
API_KEY = os.getenv("PALACE_API_KEY", "")

CONFIG_PATH = os.path.expanduser(
    os.getenv("MEMPALACE_CONFIG", "~/.mempalace/config.json")
)
# Valid mcp_mode values. Anything else (unset, typo, garbled) falls open to "all".
VALID_MCP_MODES = ("all", "cli-only")

CLI_ONLY_REJECT_MESSAGE = (
    "MCP tools are disabled (mcp_mode=cli-only). Use the mempalace CLI, "
    "or set mcp_mode=all and reconnect."
)


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
        response = handle_line(request)
        if response is not None and request.get("id") is not None:
            print(json.dumps(response), flush=True)


def run_daemon_mode(daemon_url: str, mcp_mode: str = "all"):
    def handle(request):
        if mcp_mode == "cli-only":
            method = request.get("method")
            # tools/list → advertise zero tools without forwarding, so the
            # MCP client caches an empty surface (reclaiming ~9k tokens).
            if method == "tools/list":
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "result": {"tools": []}}
            # tools/call → reject; nothing was advertised, so nothing runs.
            if method == "tools/call":
                return {"jsonrpc": "2.0", "id": request.get("id"),
                        "error": {"code": -32601,
                                  "message": CLI_ONLY_REJECT_MESSAGE}}
            # initialize, ping, notifications/*, resources/list, prompts/list
            # fall through to normal forwarding so the server still shows
            # Connected (with 0 tools).
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

    if find_daemon(args.daemon):
        # Log connection success to stderr so it doesn't break JSON-RPC stdout
        mcp_mode = resolve_mcp_mode()
        print(f"palace-daemon: connected at {args.daemon} (mcp_mode={mcp_mode})", file=sys.stderr)
        run_daemon_mode(args.daemon, mcp_mode)
    else:
        print(f"ERROR: palace-daemon unreachable at {args.daemon}. Direct fallback disabled for safety.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
