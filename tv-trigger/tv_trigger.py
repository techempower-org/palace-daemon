#!/usr/bin/env python3
"""tv-trigger — tiny LAN endpoint that lights the 0lympus TV on demand.

Purpose: the M5Stick voice sticks talk to the host over WiFi but the automation
router binds localhost only. This is a minimal, isolated LAN service so a stick
button (long-press KEY2) can TOGGLE the TV output. See ~/bin/TV-NOTES.md.

Toggle semantics (2026-09-16): if DP-1 reads connected -> run `tv-off`
(force the port off), else -> run `tv-on` (probe + light it). Why a toggle:
powering the TV off while DP-1 is still driven drops the DP link, i915 probes
the dying link and the box hard-freezes (watchdog defeated). So the routine is:
hold the button BEFORE the TV remote's off, and hold it again after turning it on.

Safety:
  - Token-gated (?k=...) so random LAN hosts can't bounce the greeter.
  - Cooldown: repeated hits within COOLDOWN_S collapse to one action, so
    button-mashing (or a stuck remote) can't loop `systemctl restart gdm3`.
  - Only ever runs the fixed `tv-on` / `tv-off` commands; no arbitrary input is executed.
Endpoints (all token-gated): /tv-on and /tv-wake (legacy stick URL) and
/tv-toggle all TOGGLE; /tv-force-on and /tv-force-off do exactly that.
Disable anytime: `sudo systemctl disable --now tv-trigger`.
"""
import glob
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

BIND = os.getenv("TV_TRIGGER_BIND", "0.0.0.0")
PORT = int(os.getenv("TV_TRIGGER_PORT", "8099"))
TOKEN = os.getenv("TV_TRIGGER_TOKEN", "")          # required; set in the unit
COOLDOWN_S = int(os.getenv("TV_TRIGGER_COOLDOWN", "15"))
TV_ON = "/home/radu/bin/tv-on"
TV_OFF = "/home/radu/bin/tv-off"


def _dp1_connected() -> bool:
    """True if the kernel considers DP-1 connected (card index varies by kernel).
    We key the toggle on `status`, not `enabled`: `tv-off` flips status to
    'disconnected' immediately, while mutter may leave `enabled` stale for a while."""
    for conn in glob.glob("/sys/class/drm/card*-DP-1"):
        try:
            with open(os.path.join(conn, "status")) as f:
                return f.read().strip() == "connected"
        except OSError:
            pass
    return False

_last_run = 0.0
_last_result = "never run"


def _run(action: str) -> str:
    """action: 'toggle' | 'on' | 'off'. Cooldown applies to all of them."""
    global _last_run, _last_result
    now = time.monotonic()
    if now - _last_run < COOLDOWN_S:
        return f"cooldown ({int(COOLDOWN_S - (now - _last_run))}s left): {_last_result}"
    _last_run = now
    if action == "toggle":
        action = "off" if _dp1_connected() else "on"
    cmd = TV_OFF if action == "off" else TV_ON
    try:
        out = subprocess.run([cmd], capture_output=True, text=True, timeout=40)
        text = (out.stdout + out.stderr).strip()
        _last_result = text.splitlines()[-1] if text else f"exit {out.returncode}"
    except subprocess.TimeoutExpired:
        _last_result = f"{os.path.basename(cmd)} timed out"
    print(f"{action}: {_last_result}", flush=True)
    return _last_result


class Handler(BaseHTTPRequestHandler):
    def _reply(self, code, body):
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write((body + "\n").encode())

    def do_GET(self):
        u = urlparse(self.path)
        routes = {"/tv-on": "toggle", "/tv-wake": "toggle", "/tv-toggle": "toggle",
                  "/tv-force-on": "on", "/tv-force-off": "off"}
        action = routes.get(u.path.rstrip("/"))
        if action is None:
            return self._reply(404, "not found")
        if not TOKEN or parse_qs(u.query).get("k", [""])[0] != TOKEN:
            return self._reply(403, "forbidden")
        return self._reply(200, _run(action))

    def log_message(self, *a):  # quiet; journald captures explicit prints
        pass


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("refusing to start without TV_TRIGGER_TOKEN")
    print(f"tv-trigger on {BIND}:{PORT}, cooldown={COOLDOWN_S}s", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
