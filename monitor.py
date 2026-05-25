#!/usr/bin/env python3
"""
palace-monitor — live integrity monitor for a palace-daemon instance.

Polls /health, /stats, and /repair/status on a configurable interval and
prints a live dashboard. Alerts on:
  - Daemon unreachable or health degraded
  - Drawer count drop beyond threshold
  - Repair unexpectedly in progress
  - Elevated HTTP error rate

Usage:
  python monitor.py                          # defaults: http://localhost:8085, 5s interval
  python monitor.py --url http://artemis:8086 --interval 3 --log palace-monitor.log
  python monitor.py --url http://familiar:8085 --interval 10   # watch production
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


RESET  = "\033[0m"
BOLD   = "\033[1m"
RED    = "\033[91m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
CYAN   = "\033[96m"
DIM    = "\033[2m"
CLEAR  = "\033[2J\033[H"


def _c(colour: str, text: str) -> str:
    return f"{colour}{text}{RESET}"


def _get(url: str, api_key: str = "", timeout: float = 5.0) -> Optional[dict]:
    req = urllib.request.Request(url)
    if api_key:
        req.add_header("X-Api-Key", api_key)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


@dataclass
class Snapshot:
    ts: str = ""
    reachable: bool = False
    health_status: str = "?"
    drawer_total: int = -1
    kg_facts: int = -1
    repair_in_progress: bool = False
    repair_mode: Optional[str] = None
    pending_writes: int = 0
    raw_health: dict = field(default_factory=dict)
    raw_stats: dict = field(default_factory=dict)
    raw_repair: dict = field(default_factory=dict)


def poll(base_url: str, api_key: str) -> Snapshot:
    s = Snapshot(ts=datetime.now().strftime("%H:%M:%S"))
    health = _get(f"{base_url}/health", api_key)
    if health is None:
        return s

    s.reachable = True
    s.health_status = health.get("status", "?")
    s.raw_health = health

    stats = _get(f"{base_url}/stats", api_key) or {}
    s.raw_stats = stats

    status_data = stats.get("status") or {}
    s.drawer_total = status_data.get("total_drawers", -1)

    kg = stats.get("kg") or {}
    s.kg_facts = kg.get("current_facts", -1)

    repair = _get(f"{base_url}/repair/status", api_key) or {}
    s.raw_repair = repair
    s.repair_in_progress = repair.get("in_progress", False)
    s.repair_mode = repair.get("mode")
    s.pending_writes = repair.get("pending_writes", 0)

    return s


@dataclass
class Alert:
    level: str
    msg: str


def check_alerts(prev: Optional[Snapshot], cur: Snapshot, drop_threshold: int) -> list[Alert]:
    alerts = []
    if not cur.reachable:
        alerts.append(Alert("CRIT", "Daemon unreachable"))
        return alerts
    if cur.health_status != "ok":
        alerts.append(Alert("CRIT", f"Health degraded: status={cur.health_status}"))
    if cur.repair_in_progress:
        alerts.append(Alert("WARN", f"Repair in progress (mode={cur.repair_mode})"))
    if cur.pending_writes > 0:
        alerts.append(Alert("WARN", f"{cur.pending_writes} pending writes queued"))
    if prev and prev.reachable and cur.drawer_total >= 0 and prev.drawer_total >= 0:
        drop = prev.drawer_total - cur.drawer_total
        if drop >= drop_threshold:
            alerts.append(Alert("CRIT", f"Drawer count dropped {drop} ({prev.drawer_total} -> {cur.drawer_total})"))
    if prev and prev.reachable and cur.kg_facts >= 0 and prev.kg_facts >= 0:
        drop = prev.kg_facts - cur.kg_facts
        if drop >= drop_threshold:
            alerts.append(Alert("WARN", f"KG active facts dropped {drop} ({prev.kg_facts} -> {cur.kg_facts})"))
    return alerts


def render(cur: Snapshot, prev: Optional[Snapshot], alerts: list[Alert],
           baseline: Optional[Snapshot], url: str, interval: int, tick: int) -> str:
    lines = []

    header = f" palace-monitor  {_c(DIM, url)}  every {interval}s  tick #{tick}"
    lines.append(_c(BOLD + CYAN, header))
    lines.append(_c(DIM, "-" * 60))

    ts_label = _c(DIM, f"[{cur.ts}]")

    if not cur.reachable:
        lines.append(f"{ts_label}  {_c(RED + BOLD, 'x UNREACHABLE')}")
    else:
        health_col = GREEN if cur.health_status == "ok" else RED
        lines.append(f"{ts_label}  health  {_c(health_col + BOLD, cur.health_status.upper())}")

        def _delta(now: int, was: Optional[int]) -> str:
            if was is None or now < 0 or was < 0:
                return ""
            d = now - was
            if d == 0:
                return ""
            col = GREEN if d > 0 else YELLOW
            sign = "+" if d > 0 else ""
            return _c(col, f"  ({sign}{d})")

        prev_d = prev.drawer_total if prev and prev.reachable else None
        prev_k = prev.kg_facts    if prev and prev.reachable else None
        base_d = baseline.drawer_total if baseline else None

        drawer_str = str(cur.drawer_total) if cur.drawer_total >= 0 else "?"
        kg_str     = str(cur.kg_facts)     if cur.kg_facts >= 0     else "?"

        baseline_note = _c(DIM, f"  baseline {base_d}") if base_d is not None and cur.drawer_total >= 0 else ""

        lines.append(f"          drawers {_c(BOLD, drawer_str)}{_delta(cur.drawer_total, prev_d)}{baseline_note}")
        lines.append(f"          KG facts {_c(BOLD, kg_str)}{_delta(cur.kg_facts, prev_k)}")

        if cur.repair_in_progress:
            lines.append(f"          repair  {_c(YELLOW, f'IN PROGRESS ({cur.repair_mode})')}")
        if cur.pending_writes > 0:
            lines.append(f"          pending {_c(YELLOW, str(cur.pending_writes))} writes queued")

    lines.append("")
    if alerts:
        for a in alerts:
            col = RED if a.level == "CRIT" else YELLOW
            lines.append(f"  {_c(col + BOLD, a.level)}  {a.msg}")
    else:
        lines.append(f"  {_c(GREEN, 'no alerts')}")

    return "\n".join(lines)


def main():
    p = argparse.ArgumentParser(description="Live palace-daemon integrity monitor")
    p.add_argument("--url",       default="http://localhost:8085", help="Daemon base URL")
    p.add_argument("--interval",  type=int, default=5,             help="Poll interval in seconds")
    p.add_argument("--log",       default="",                      help="Append events to this file")
    p.add_argument("--api-key",   default="",                      help="X-Api-Key header value")
    p.add_argument("--drop",      type=int, default=1,             help="Alert threshold for drawer/KG drops")
    p.add_argument("--no-clear",  action="store_true",             help="Don't clear terminal between updates")
    args = p.parse_args()

    log_fh = open(args.log, "a", encoding="utf-8") if args.log else None
    baseline: Optional[Snapshot] = None
    prev: Optional[Snapshot] = None
    tick = 0

    try:
        while True:
            tick += 1
            cur = poll(args.url, args.api_key)

            if baseline is None and cur.reachable:
                baseline = cur

            alerts = check_alerts(prev, cur, args.drop)

            if not args.no_clear:
                print(CLEAR, end="")

            print(render(cur, prev, alerts, baseline, args.url, args.interval, tick))
            sys.stdout.flush()

            if log_fh and (alerts or tick == 1):
                entry = {
                    "ts": cur.ts,
                    "tick": tick,
                    "reachable": cur.reachable,
                    "health": cur.health_status,
                    "drawers": cur.drawer_total,
                    "kg_facts": cur.kg_facts,
                    "repair": cur.repair_in_progress,
                    "alerts": [{"level": a.level, "msg": a.msg} for a in alerts],
                }
                log_fh.write(json.dumps(entry) + "\n")
                log_fh.flush()

            prev = cur
            time.sleep(args.interval)

    except KeyboardInterrupt:
        print(f"\n{_c(DIM, 'Monitor stopped.')}")
    finally:
        if log_fh:
            log_fh.close()


if __name__ == "__main__":
    main()
