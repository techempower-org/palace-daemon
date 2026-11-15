#!/usr/bin/env python3
"""CHECK redact-needles: can the palace still write, and did a vault needle stop it?

mempalace.redact (mempalace #619) can load vault items as needles: the config lists item ids and
labels (``secret_vault_items``), the values are fetched in process, and a value is redacted on word
boundaries. An item that cannot be fetched (a locked vault) fails every write CLOSED: mining stops
rather than store the password again. That is the intended trade (a stale palace is visible and
recoverable, a re-mined password is not), and this check is what makes it visible.

Two instruments:
- a probe: load the redaction patterns in this process exactly as a write does (it fetches every
  configured needle); a failure here is a write that would be refused now;
- the daemon's journal over the last ``WINDOW_MIN`` minutes: writes refused with
  ``SecretPatternsUnavailable``, by needle label.

- **fail**: the probe raises, or writes were refused in the window. The line names the label
  only ("mining stopped: vault item <label>: vault answered rc 1, empty"), never a value.
- **warn**: no needle configured while the config asks for one (``REDACT_NEEDLES_EXPECT``), or the
  journal could not be read.
- **ok**: n needle(s) loaded (labels), 0 refused writes in the window.

The miss behind it (2026-11-15): the shared homelab password, lower case letters only, matched no
secret shape and sat in ~950 drawers; the needle that now catches it can stop all mining when the
vault locks, and nobody would know without a line on the Watch.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from checklib import emit, label, line, run

NAME = "palace-redact-needles"
WINDOW_MIN = 20
_REFUSED = re.compile(r"SecretPatternsUnavailable|secret patterns? (?:unavailable|does not)|vault item [a-z0-9-]+:")
_LABEL = re.compile(r"vault item ([a-z0-9-]+)")


def refused_writes(journal: list[str]) -> tuple[int, list[str]]:
    """(refused-write lines, needle labels named in them, in order of first appearance)."""
    n, labels = 0, []
    for row in journal:
        if _REFUSED.search(row):
            n += 1
            for m in _LABEL.finditer(row):
                if m.group(1) not in labels:
                    labels.append(m.group(1))
    return n, labels


def assess(probe_error: str | None, needles: list[str], journal: list[str] | None, expect: int) -> tuple[str, str]:
    """State and fact from the probe's error (None = loaded), the configured labels, the window's
    journal (None = unreadable) and how many needles the host expects."""
    names = ", ".join(label(x) for x in needles) or "none"
    if probe_error is not None:
        m = _LABEL.search(probe_error)
        who = f"vault item {m.group(1)}" if m else "redaction patterns"
        return "fail", f"mining stopped: {who} cannot load now ({label(probe_error.split(':')[-1].strip())[:40]}); every palace write is refused until the vault answers"
    if journal is None:
        return "warn", f"{len(needles)} needle(s) loaded ({names}); the daemon journal could not be read"
    n, labels = refused_writes(journal)
    if n:
        who = ", ".join(labels) or "patterns"
        return "fail", f"{n} palace write(s) refused in the last {WINDOW_MIN} min ({who}); needles load now, so it recovered or is intermittent"
    if expect and len(needles) < expect:
        return "warn", f"{len(needles)} needle(s) configured, {expect} expected ({names})"
    return "ok", f"{len(needles)} needle(s) loaded ({names}); 0 palace writes refused in the last {WINDOW_MIN} min"


def _probe() -> tuple[str | None, list[str]]:
    from mempalace.config import MempalaceConfig
    from mempalace.redact import SecretPatternsUnavailable, patterns

    items = MempalaceConfig()._file_config.get("secret_vault_items", []) or []
    labels = [str(it.get("label", "?")) for it in items if isinstance(it, dict)]
    try:
        patterns()
    except SecretPatternsUnavailable as exc:
        return str(exc), labels
    return None, labels


def _journal() -> list[str] | None:
    try:
        out = subprocess.run(
            ["journalctl", "-u", "palace-daemon", "--since", f"-{WINDOW_MIN}min", "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.splitlines() if out.returncode == 0 else None


def body() -> None:
    err, labels = _probe()
    expect = int(os.environ.get("REDACT_NEEDLES_EXPECT", "0") or 0)
    state, fact = assess(err, labels, _journal(), expect)
    nxt = "owner palace lane: unlock the vault (bw), then the next write loads the needles" if state == "fail" else "owner palace lane"
    emit(NAME, line(NAME, state, fact, nxt))


if __name__ == "__main__":
    sys.exit(run(NAME, body))
