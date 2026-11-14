#!/usr/bin/env python3
"""CHECK gs-auth: on each desktop, is gnome-speaks up, in the auth state it should be, with no
caller being refused, and able to find ``bw`` when started at boot?

``GS_CHECK_HOSTS`` lists ``host=required`` or ``host=open`` (the expected token state), space
separated; hosts are read over ssh, never named in this repo. Per host, read on the host itself:
the unit's state; ``/status`` with the host's own token (curl -K, never argv), and only when its
curl config holds a header, so this check never sends a tokenless request; the "refused:" and
start lines in the unit's journal; and whether the deployed service resolves ``bw`` beyond PATH.

- fail: a host unreachable, the unit not active, no token config, or /status unreadable
- warn: callers refused in the last ``WINDOW_HOURS``; the token state differs from the expected
  one; the journal could not be read; the deployed service predates the boot-PATH fix.
  Refusals within ``DEPLOY_GRACE`` s after a service start are a deploy's own tokenless probes
  (the restart recipe checks 401 right away): counted apart, not warned on. Starts are read from
  ``DEPLOY_GRACE`` s before the window, so a probe just inside it is still matched to its start.
- ok otherwise

The misses behind it: 2026-11-13, a caller the audit missed would have been refused silently
after the token flip; 2026-11-14, a desktop's unit started at boot could not find ``bw``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, label, line, run

NAME = "palace-gs-auth"
WINDOW_HOURS = 24
DEPLOY_GRACE = 60
_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")

# Runs on the host (bash -s); prints one JSON object. No token in argv: curl reads curl.conf.
REMOTE = r"""
set -u
conf="$HOME/.config/gnome-speaks/curl.conf"
svc=$(systemctl --user show gnome-speaks -p ExecStart --value 2>/dev/null | sed -n 's/.*path=\([^ ;]*\).*/\1/p' | head -1)
active=$(systemctl --user is-active gnome-speaks 2>/dev/null)
tokenconf=0; status=""
# Only the token header is taken from the host's curl config, and handed to curl on stdin as a
# one-line config: the rest of that file (urls, `next`, options) is never executed.
hdr=$( [ -r "$conf" ] && sed -nE 's/^[[:space:]]*(header|-H)[[:space:]=]+"?((X-Speaks-Token|Authorization):[^"]*)"?[[:space:]]*$/\2/Ip' "$conf" | head -1 )
if printf '%s' "$hdr" | grep -Eiq '^(X-Speaks-Token:[[:space:]]*|Authorization:[[:space:]]*Bearer[[:space:]]+)[^[:space:]"]{8,}$'; then
  tokenconf=1
  status=$(printf 'header = "%s"\n' "$hdr" | curl -q -s -m 5 -K - http://127.0.0.1:7710/status 2>/dev/null || true)
fi
now=$(date +%s)
if raw=$(journalctl --user -u gnome-speaks --since "-SPANs" --no-pager -o short-unix 2>/dev/null); then journal=1; else journal=0; raw=""; fi
events=$(printf '%s\n' "$raw" | grep -E 'refused:|HTTP token loaded' | awk '{print $1, ($0 ~ /HTTP token loaded/ ? "start" : "refused")}' | tr '\n' ';')
bwfix=0; [ -n "$svc" ] && [ -r "$svc" ] && bwfix=$(grep -c 'def _bw_path' "$svc")
python3 -c 'import json,sys
a=sys.argv
try: st=json.loads(a[2]) if a[2] else None
except ValueError: st=None
ev=[e.split() for e in a[3].split(";") if len(e.split())==2]
print(json.dumps({"active":a[1],"status":st,"events":[[float(t),k] for t,k in ev],"bwfix":int(a[4] or 0),
  "tokenconf":a[5]=="1","journal":a[6]=="1","now":float(a[7])}))' \
  "$active" "$status" "$events" "$bwfix" "$tokenconf" "$journal" "$now"
"""


def expectations(spec: str) -> tuple[list[tuple[str, str, str]], list[int]]:
    """(``host=required|open[@label]`` items as (host, state, label), the 1-based positions of
    items not understood). A host must be a plain name (never an ssh option); one bad item fails
    the check rather than quietly dropping a host. The label is what the line shows (default
    ``desktop-<n>``); a label that equals any configured host is refused, so the line, which
    goes to the plan's page, never carries a host name."""
    items = spec.split()
    hosts = {it.partition("=")[0] for it in items}
    out, bad = [], []
    for n, item in enumerate(items, 1):
        host, _, rest = item.partition("=")
        want, _, name = rest.partition("@")
        ok = _HOST.match(host) and want in ("required", "open")
        name = name or f"desktop-{n}"
        ok = (
            ok and _HOST.match(name) and name not in hosts
        )  # the label shown, default included
        if ok:
            out.append((host, want, name))
        else:
            bad.append(n)
    return out, bad


def caller_refusals(
    events, window_start: float | None = None
) -> tuple[int, int, float | None]:
    """(refusals from callers, refusals that were a deploy's probes, time of the last caller
    one) from [unix_time, "start"|"refused"] events. Refusals before ``window_start`` are not
    counted; starts before it still match a probe."""
    callers = probes = 0
    last_start = None
    last_caller = None
    for t, kind in sorted(
        (float(t), str(k)) for t, k in events or [] if isinstance(t, (int, float))
    ):
        if kind == "start":
            last_start = t
        elif kind == "refused":
            if window_start is not None and t < window_start:
                continue
            if last_start is not None and 0 <= t - last_start <= DEPLOY_GRACE:
                probes += 1
            else:
                callers += 1
                last_caller = t
    return callers, probes, last_caller


def judge_host(host: str, want: str, r: dict | None) -> tuple[str, str]:
    """(state, fact) for one host; ``host`` here is the label the line may show."""
    host = label(host)
    if not isinstance(r, dict):
        return "fail", f"{host} unreachable"
    if r.get("active") != "active":
        return "fail", f"{host} unit {label(r.get('active') or 'unknown')}"
    if not r.get("tokenconf"):
        return (
            "fail",
            f"{host} has no token in its curl config (not probed: never tokenless)",
        )
    st = r.get("status")
    auth = st.get("auth") if isinstance(st, dict) else None
    if not isinstance(auth, dict) or not isinstance(auth.get("required"), bool):
        return "fail", f"{host} /status unreadable"
    req = "required" if auth["required"] else "open"
    now = r.get("now") if isinstance(r.get("now"), (int, float)) else None
    start = now - WINDOW_HOURS * 3600 if now is not None else None
    n, probes, last = caller_refusals(r.get("events"), start)
    fact = f"{host} {req}, {n} caller(s) refused/{WINDOW_HOURS}h"
    if n and last is not None:
        fact += f" (last {dt.datetime.fromtimestamp(last).astimezone():%a %H:%M})"
    if probes:
        fact += f", {probes} deploy probe(s)"
    issues = []
    if req != want:
        issues.append(f"expected {want}")
    if n:
        issues.append("a caller is refused")
    if not r.get("journal"):
        issues.append("journal unreadable, refusals unknown")
    if not r.get("bwfix"):
        issues.append("service predates the boot-PATH bw fix")
    if issues:
        return "warn", fact + " [" + "; ".join(issues) + "]"
    return "ok", fact


def assess(results: list[tuple[str, str, dict | None]], now: dt.datetime) -> str:
    if not results:
        return line(
            NAME,
            "fail",
            f"GS_CHECK_HOSTS lists no host (read {now:%H:%M})",
            "owner palace lane",
        )
    judged = [judge_host(h, w, r) for h, w, r in results]
    rank = {"ok": 0, "warn": 1, "fail": 2}
    state = max((s for s, _ in judged), key=rank.get)
    fact = "; ".join(f for _, f in judged) + f" (read {now:%H:%M})"
    nxt = {
        "ok": "owner palace lane",
        "warn": "owner palace lane: find the refused caller in that host's journal, or align the unit/code",
        "fail": "owner palace lane: restart gnome-speaks on that host or restore its token config, then re-run this check",
    }[state]
    return line(NAME, state, fact, nxt)


def read_host(host: str) -> dict | None:
    if not _HOST.match(host):
        return None
    try:
        p = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=8",
                "--",
                host,
                "bash",
                "-s",
            ],
            input=REMOTE.replace("SPAN", str(WINDOW_HOURS * 3600 + DEPLOY_GRACE)),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        last = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        doc = json.loads(last) if p.returncode == 0 and last else None
        return doc if isinstance(doc, dict) else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def body() -> None:
    now = dt.datetime.now().astimezone()
    hosts, bad = expectations(os.environ.get("GS_CHECK_HOSTS", ""))
    if bad:
        emit(
            NAME,
            line(
                NAME,
                "fail",
                f"GS_CHECK_HOSTS item(s) {','.join(map(str, bad))} not understood (read {now:%H:%M})",
                "owner palace lane: fix ~/.config/palace-checks/env",
            ),
        )
        return
    emit(NAME, assess([(name, w, read_host(h)) for h, w, name in hosts], now))


def main() -> int:
    return run(NAME, body)


if __name__ == "__main__":
    sys.exit(main())
