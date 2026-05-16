#!/usr/bin/env python3
"""
palace-daemon hook runner — stdlib-only replacement for `mempalace hook run`.

Routes all mine operations through palace-daemon (POST /mine).
Never spawns mempalace as a subprocess or accesses the database directly.
If the daemon is unreachable, passes through silently — no fallback to direct access.

Mine operations require explicit user approval via a block response before firing.
MEMPAL_DIR env var controls what directory to mine; if unset, no mine is triggered.

Usage:
    python3 hook.py --hook stop        --harness claude-code
    python3 hook.py --hook precompact  --harness claude-code
    python3 hook.py --hook session-start --harness codex

Settings: ~/.mempalace/hook_settings.json
    daemon_url        (default: http://localhost:8085)
    silent_save       (default: true)  — pass through after diary save; false = block for manual save
    desktop_toast     (default: false) — fire notify-send on save triggers
    force_on_stop     (default: true)  — save at session end even with few exchanges (≥FORCE_MIN_INTERVAL s between saves)
    force_min_interval (default: 60)   — minimum seconds between force_on_stop saves
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

SAVE_INTERVAL = 15           # count-based: save every N exchanges
TIME_SAVE_INTERVAL = 300     # time-based: save if this many seconds elapsed with unsaved exchanges
FORCE_MIN_INTERVAL = 60      # force_on_stop: minimum seconds between saves (prevents per-response spam)
CHECKPOINT_TOPIC = "checkpoint"  # keep in sync with main.py and mempal-fast.py — used by kind= search filter
STATE_DIR = Path.home() / ".mempalace" / "hook_state"
HOOK_SETTINGS_PATH = Path.home() / ".mempalace" / "hook_settings.json"

# Canonical topic name for Stop-hook auto-save checkpoint diary entries.
# Defined as a constant so all hook code paths agree on the string value
# used downstream by mempalace.searcher.build_where_filter for kind=
# filtering (jphein/mempalace fork-ahead row 21, 2026-04-25). Keep in
# sync with mempal-fast.py and mempalace.hooks_cli.
CHECKPOINT_TOPIC = "checkpoint"

SUPPORTED_HARNESSES = {"claude-code", "codex", "gemini-cli"}

STOP_BLOCK_REASON = (
    "AUTO-SAVE checkpoint (MemPalace). Save this session's key content:\n"
    "1. mempalace_diary_write — AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — verbatim quotes, decisions, code snippets\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Continue conversation after saving."
)

PRECOMPACT_BLOCK_REASON = (
    "COMPACTION IMMINENT (MemPalace). Save ALL session content before context is lost:\n"
    "1. mempalace_diary_write — thorough AAAK-compressed session summary\n"
    "2. mempalace_add_drawer — ALL verbatim quotes, decisions, code, context\n"
    "3. mempalace_kg_add — entity relationships (optional)\n"
    "Be thorough — after compaction, detailed context will be lost. "
    "Do NOT write to Claude Code's native auto-memory (.md files). "
    "Save everything to MemPalace, then allow compaction to proceed."
)


def _mine_approval_reason(mine_dir: str, daemon_url: str) -> str:
    return (
        f"AUTO-INGEST requested (MemPalace).\n"
        f"Target directory: {mine_dir}\n\n"
        f"Show the user this directory and ask them to approve or deny mining it into the palace.\n"
        f"  Approve → POST {{\"dir\": \"{mine_dir}\", \"mode\": \"convos\"}} to {daemon_url}/mine\n"
        f"  Deny    → inform user, continue."
    )


def _sanitize_session_id(session_id: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "", session_id)
    return sanitized or "unknown"


def _validate_transcript_path(transcript_path: str) -> Path:
    if not transcript_path:
        return None
    path = Path(transcript_path).expanduser().resolve()
    if path.suffix not in (".jsonl", ".json"):
        return None
    if ".." in Path(transcript_path).parts:
        return None
    return path


_state_dir_initialized = False
_LOG_MAX_BYTES = 10 * 1024 * 1024  # rotate at 10 MB
_LOG_KEEP = 3                       # hook.log.1 .. hook.log.3, then drop


def _rotate_log_if_needed(log_path: Path):
    """Size-gated rotation, run before each append.

    When ``log_path`` exceeds _LOG_MAX_BYTES, shift .1→.2, .2→.3, drop the
    oldest, rename current → .1, and let the next write start fresh.
    Cheap to call every time: usually just a single ``stat`` syscall.
    """
    try:
        if not log_path.exists() or log_path.stat().st_size < _LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        oldest = log_path.with_name(f"{log_path.name}.{_LOG_KEEP}")
        if oldest.exists():
            try:
                oldest.unlink()
            except OSError:
                pass
        for i in range(_LOG_KEEP - 1, 0, -1):
            src = log_path.with_name(f"{log_path.name}.{i}")
            dst = log_path.with_name(f"{log_path.name}.{i + 1}")
            if src.exists():
                try:
                    src.rename(dst)
                except OSError:
                    pass
        try:
            log_path.rename(log_path.with_name(f"{log_path.name}.1"))
        except OSError:
            pass
    except OSError:
        pass


def _log(message: str):
    global _state_dir_initialized
    try:
        if not _state_dir_initialized:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            try:
                STATE_DIR.chmod(0o700)
            except (OSError, NotImplementedError):
                pass
            _state_dir_initialized = True
        log_path = STATE_DIR / "hook.log"
        _rotate_log_if_needed(log_path)
        is_new = not log_path.exists()
        timestamp = datetime.now().strftime("%H:%M:%S")
        with open(log_path, "a") as f:
            f.write(f"[{timestamp}] {message}\n")
        if is_new:
            try:
                log_path.chmod(0o600)
            except (OSError, NotImplementedError):
                pass
    except OSError:
        pass


def _output(data: dict):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _detach_for_async_work() -> bool:
    """Fork + setsid + redirect FDs to /dev/null. Returns ``True`` if
    we are the (detached) child and should continue with the slow
    work; ``False`` if we are the parent and should ``return``.

    Used by ``hook_stop`` / ``hook_precompact`` *after* they've emitted
    their user-visible ``systemMessage`` via ``_output`` — claude waits
    for the hook's stdout/stderr pipes to close before clearing the
    event, so the parent's ``return`` lets the harness unblock while
    the child finishes the slow daemon round-trip. Emitting the themed
    message *before* the fork is the whole point of this helper: the
    detached child's stdout is ``/dev/null``, so any ``_output`` call
    after detach is invisible.

    Failures are conservative — if ``os.fork()`` raises, we return
    ``True`` so the caller runs inline (synchronous fallback). Set
    ``PALACE_HOOK_NO_DETACH=1`` to skip the detach entirely (testing).
    """
    if os.environ.get("PALACE_HOOK_NO_DETACH") == "1":
        return True
    try:
        pid = os.fork()
    except OSError:
        return True  # fork failed; run inline as a fallback
    if pid > 0:
        return False  # parent: caller should immediately return
    # We are the child. Detach so claude's harness pipe-close logic
    # doesn't block on us.
    try:
        os.setsid()
        devnull_in = os.open(os.devnull, os.O_RDONLY)
        devnull_out = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_in, 0)
        os.dup2(devnull_out, 1)
        os.dup2(devnull_out, 2)
        os.close(devnull_in)
        os.close(devnull_out)
    except OSError:
        pass
    return True


def _read_last_save_ts(session_id: str) -> float:
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        return float(ts_file.read_text().strip())
    except Exception:
        return 0.0


def _write_last_save_ts(session_id: str):
    ts_file = STATE_DIR / f"{session_id}_last_save_ts"
    try:
        ts_file.write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def _load_hook_settings() -> dict:
    try:
        return json.loads(HOOK_SETTINGS_PATH.read_text())
    except Exception:
        return {}


def _count_human_messages(transcript_path: str) -> int:
    """Count real user turns in a transcript, excluding tool-result roundtrips.

    Claude Code's messages API frames tool results as ``role: "user"``
    messages whose content is a list of ``{type: "tool_result", ...}``
    blocks — conceptually "the user delivering the tool's output back to
    the model." These aren't human exchanges; counting them inflates
    every save interval by 5–10×.

    Mirrors upstream issue MemPalace/mempalace#549 — same bug in upstream's
    ``hooks_cli._count_human_messages``. Fixed locally first per JP's
    review-before-upstream policy.

    Rules:
      - ``role == "user"`` with string content → count (unless ``<command-message>``)
      - ``role == "user"`` with list content → count only if it has at
        least one ``type: "text"`` block (and that block isn't ``<command-message>``)
      - Codex ``event_msg`` user_message branch unchanged (uses an
        explicit user_message type, so it never has the tool-result
        ambiguity)
    """
    path = _validate_transcript_path(transcript_path)
    if path is None:
        return 0
    if not path.is_file():
        return 0
    count = 0
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                    msg = entry.get("message", {})
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = msg.get("content", "")
                        if isinstance(content, str):
                            if not content.strip():
                                continue
                            if "<command-message>" in content:
                                continue
                            count += 1
                        elif isinstance(content, list):
                            # A real human turn has at least one text block.
                            # All-tool_result content is a tool roundtrip,
                            # not a human exchange — skip.
                            text_blocks = [
                                b for b in content
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            if not text_blocks:
                                continue
                            joined = " ".join(b.get("text", "") for b in text_blocks)
                            if not joined.strip():
                                continue
                            if "<command-message>" in joined:
                                continue
                            count += 1
                    elif entry.get("type") == "event_msg":
                        payload = entry.get("payload", {})
                        if isinstance(payload, dict) and payload.get("type") == "user_message":
                            msg_text = payload.get("message", "")
                            if isinstance(msg_text, str) and "<command-message>" not in msg_text:
                                count += 1
                except (json.JSONDecodeError, AttributeError):
                    pass
    except OSError:
        return 0
    return count


def _get_mine_dir() -> str:
    """Return mine directory from MEMPAL_DIR only. No transcript path fallback."""
    mempal_dir = os.environ.get("MEMPAL_DIR", "")
    if mempal_dir and os.path.isdir(mempal_dir):
        return mempal_dir
    return ""


def _slugify_project(name: str) -> str:
    """Lower snake_case slug. Dashes, dots, spaces collapse to underscores.
    Anything else non-alnum is dropped. Per the room taxonomy spec
    (familiar.realm.watch docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md)
    wing slugs are project-derived and use lower snake_case.
    """
    s = name.strip().lower()
    s = re.sub(r"[-.\s]+", "_", s)
    s = re.sub(r"[^a-z0-9_]", "", s)
    return s.strip("_")


def _decode_project_id(project_id: str) -> str:
    """Best-effort decode of Claude Code's ~/.claude/projects/<id> directory name.

    Claude Code encodes the project directory by replacing `/` and `.` with
    `-`, which is destructive — we can't perfectly reconstruct the path.
    But the *last* segment is what we want as the wing slug, and the
    encoding is consistent: take everything after the last `Projects-`
    marker. That sidesteps the path-vs-dotted-name ambiguity for anything
    living under ~/Projects/.

    Examples:
      -home-jp-Projects-familiar-realm-watch → familiar-realm-watch
      -home-jp-Projects-palace-daemon         → palace-daemon
      -home-jp-Projects-memorypalace          → memorypalace
    Fallback: return the raw segment after the leading dash.
    """
    if not project_id:
        return ""
    s = project_id.lstrip("-")
    marker = "Projects-"
    idx = s.find(marker)
    if idx >= 0:
        return s[idx + len(marker):]
    return s


def _project_wing(data: dict, transcript_path: str) -> str:
    """Resolve the wing slug for this session's writes.

    Per the room taxonomy (Project-Topic-Drawer model): wing = project,
    not agent. The hook discovers the project by walking these sources
    in order:

      1. data["cwd"] — Claude Code 2.1+ passes the session cwd on stdin.
      2. transcript_path filename — Claude Code encodes the project root
         in the parent directory name under ~/.claude/projects/.
      3. os.getcwd() — last resort, may be wherever the hook was spawned.

    Returns the bare project slug (e.g. ``familiar_realm_watch``), NO
    ``wing_`` prefix. The prefix is a docstring convention in mempalace
    (mcp_server.py:727) that is *not* enforced by code, and adding it
    here was perpetuating a mixed namespace — JP's palace already has
    both ``wing_X`` and ``X`` flavors of the same projects. The spec
    explicitly rejects the prefix:
    ``familiar.realm.watch/docs/superpowers/specs/2026-05-13-palace-room-taxonomy.md``.

    Fallback: ``personal`` if no project can be detected.
    """
    cwd = (data or {}).get("cwd", "")
    if cwd:
        try:
            p = Path(cwd).expanduser().resolve()
            home_projects = (Path.home() / "Projects").resolve()
            if home_projects == p or home_projects in p.parents:
                rel = p.relative_to(home_projects)
                if rel.parts:
                    return _slugify_project(rel.parts[0])
            # Outside ~/Projects/ — use last segment
            slug = _slugify_project(p.name)
            if slug:
                return slug
        except (OSError, ValueError):
            pass

    # Fallback: decode the transcript_path parent directory
    if transcript_path:
        try:
            parent = Path(transcript_path).parent.name  # e.g. -home-jp-Projects-X
            slug = _slugify_project(_decode_project_id(parent))
            if slug:
                return slug
        except (OSError, ValueError):
            pass

    # Last resort: cwd from the process. If it's $HOME itself, call it
    # personal — the basename of $HOME is the username, not a project.
    try:
        here = Path(os.getcwd()).resolve()
        home = Path.home().resolve()
        if here == home:
            return "personal"
        slug = _slugify_project(here.name)
        if slug:
            return slug
    except OSError:
        pass

    return "personal"


def _request_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    api_key = os.environ.get("PALACE_API_KEY", "").strip()
    if api_key:
        headers["X-API-Key"] = api_key
    return headers


def _post_mcp(daemon_url: str, tool_name: str, params: dict):
    """POST a JSON-RPC tool call to /mcp.

    Returns ``(ok, response_dict_or_failure_reason)`` so callers can render
    themed feedback based on what actually happened — not just yes/no.

    - On success: ``(True, <parsed JSON-RPC response>)``
    - On HTTP error: ``(False, {"error": "HTTP <code>: <reason>"})``
    - On network/transport error: ``(False, {"error": "<exception>"})``
    """
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool_name, "arguments": params},
    }
    try:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mcp",
            data=data,
            headers=_request_headers(),
            method="POST",
        )
        # 30s timeout (was 10s before 2026-05-13): the daemon serializes
        # writes via PALACE_MAX_WRITE_CONCURRENCY=1 to avoid SIGSEGV from
        # concurrent chromadb writers. Bursts of 3+ Stop hooks within ~10s
        # would stair-step into queue waits and time out spuriously at 10s.
        # 30s lets a queue of ~5-6 saves complete before we surface a
        # "timed out" message. Real daemon hangs still surface; the wait
        # is only displaced, not hidden.
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status != 200:
                return False, {"error": f"HTTP {resp.status}"}
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                body = None
            return True, body
    except urllib.error.HTTPError as e:
        _log(f"mcp via daemon rejected (HTTP {e.code}): {e.reason}")
        return False, {"error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        _log(f"mcp via daemon failed (network/transport): {e}")
        return False, {"error": f"network/transport: {e}"}


def _post_mine(daemon_url: str, mine_dir: str, timeout: int = 60,
               mode: str = "convos", wing: str = ""):
    """POST /mine to daemon. Returns (ok, response_or_failure_reason).

    Mode defaults to ``convos`` (the only sensible default for transcript
    ingest — and matches the daemon's accepted set ``{convos, projects}``;
    the older ``"auto"`` literal was never valid and silently 400'd).
    Wing is forwarded when truthy so transcript drawers land in the right
    project wing rather than the daemon's default ``"general"``.
    """
    body = {"dir": mine_dir, "mode": mode}
    if wing:
        body["wing"] = wing
    payload = json.dumps(body).encode()
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/mine",
            data=payload,
            headers=_request_headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                return False, {"error": f"HTTP {resp.status}"}
            try:
                body = json.loads(resp.read().decode("utf-8"))
            except Exception:
                body = None
            return True, body
    except urllib.error.HTTPError as e:
        _log(f"mine via daemon rejected (HTTP {e.code} {e.reason}) — check PALACE_API_KEY")
        return False, {"error": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        _log(f"mine via daemon failed (network/transport): {e}")
        return False, {"error": f"network/transport: {e}"}


def _get_palace_stats(daemon_url: str) -> dict:
    """Quick-query the daemon's /stats endpoint to enrich themed messages.

    Returns the parsed stats dict (with drawer_count etc) or an empty dict
    on any failure — never raises. Used as a *garnish* on save/mine feedback;
    the hook proceeds even if the stats call fails.

    Short timeout (2s) because /stats touches the chromadb collection and
    can be slow under load. Themed feedback is nice-to-have; we'd rather
    skip the drawer count than make the user wait on the hook.
    """
    try:
        req = urllib.request.Request(
            daemon_url.rstrip("/") + "/stats",
            headers=_request_headers(),
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status != 200:
                return {}
            return json.loads(resp.read().decode("utf-8")) or {}
    except Exception:
        return {}


def _format_palace_count(stats: dict) -> str:
    """Render the palace drawer count for inline messaging.

    Tolerates different shapes of /stats since the daemon's response may evolve.
    Returns an empty string when no useful count is available.
    """
    if not stats:
        return ""
    # Try several plausible keys before giving up
    for key in ("drawer_count", "drawers", "total", "count", "elements"):
        if key in stats and isinstance(stats[key], int):
            return f"{stats[key]:,} drawers"
    # /stats might nest counts under a per-collection breakdown
    by_col = stats.get("collections") or {}
    if isinstance(by_col, dict):
        drawers = by_col.get("mempalace_drawers")
        if isinstance(drawers, dict) and isinstance(drawers.get("count"), int):
            return f"{drawers['count']:,} drawers"
    return ""


def _display_wing(wing: str) -> str:
    """Drop the ``wing_`` prefix for human-readable rendering.

    Per the palace room taxonomy spec, wing slugs are project-derived
    (e.g. ``familiar_realm_watch``). mempalace's chromadb era prepends
    ``wing_`` for namespacing; humans read the slug bare.
    """
    return wing[5:] if isinstance(wing, str) and wing.startswith("wing_") else wing or "?"


def _drawer_label(topic: str, timestamp: str) -> str:
    """Build a human-readable slug-style drawer label from topic + timestamp.

    Drawer IDs themselves are opaque (content-hashed for idempotency and
    collision-free generation) — see ``diary_write`` / ``add_drawer`` in
    mempalace. They're storage handles, not navigation handles. The
    themed message wants something a human can recognize at a glance,
    so we synthesize ``<topic>@<HH:MM>`` from the metadata mempalace
    already returns. The full drawer_id stays available via search /
    list_drawers when the hash matters.

    Examples:
      _drawer_label("checkpoint", "2026-05-13T08:48:16.427801") → "checkpoint@08:48"
      _drawer_label("precompact", "")                            → "precompact"
      _drawer_label("", "")                                       → "?"
    """
    topic = (topic or "").strip()
    if isinstance(timestamp, str) and "T" in timestamp:
        # ISO-8601 ``YYYY-MM-DDTHH:MM:SS.fff``; we want ``HH:MM``.
        try:
            hhmm = timestamp.split("T", 1)[1][:5]
            if topic:
                return f"{topic}@{hhmm}"
            return f"@{hhmm}"
        except (IndexError, AttributeError):
            pass
    return topic or "?"


def _theme_save_ok(exchange_count: int, trigger: str, response: dict, palace_count: str, wing: str = "") -> str:
    """Build the success themed message for a Stop-hook save.

    Renders the full chain a human-readable walk takes through the palace
    to reach the drawer that was just filed:

        palace → wing:<project> → room:sessions → drawer:…<short-id>

    Per the room taxonomy spec, the wing is the *project*, not the agent.
    The agent identity lives in drawer metadata. Room remains ``diary``
    today because ``tool_diary_write`` hardcodes it; post-pgvector
    migration this becomes room=``sessions`` per the canonical 7-room
    list. An upstream room-parameter feature request will follow once
    the taxonomy has settled in our own pgvector schema.

    Topic surfaces as a tag after the chain — present but not part of
    the path. Closets (the index layer) are auto-built by
    ``mempalace mine`` and aren't addressable from a diary write.
    """
    inner = {}
    try:
        content = response.get("result", {}).get("content", []) if isinstance(response, dict) else []
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        inner = {}

    topic = inner.get("topic", "") or ""
    timestamp = inner.get("timestamp", "") or ""
    drawer_label = _drawer_label(topic, timestamp)

    display = _display_wing(wing) if wing else _display_wing(inner.get("agent", ""))
    if display and display != "?":
        chain = f"palace → wing:{display} → room:sessions → drawer:{drawer_label}"
        head = f"✦ {chain}"
    else:
        head = "✦ Memory woven into the palace"

    tail_bits = [f"exchange {exchange_count}", f"trigger={trigger}"]
    if palace_count:
        tail_bits.append(f"palace now holds {palace_count}")
    return f"{head}  —  " + ", ".join(tail_bits)


def _theme_save_fail(exchange_count: int, trigger: str, failure: dict) -> str:
    """Build the failure themed message for a Stop-hook save."""
    err = (failure or {}).get("error", "unknown error")
    return (
        f"✘ Memory save failed at exchange {exchange_count} "
        f"(trigger={trigger}) — {err}"
    )


def _theme_mine(mine_dir: str, ok: bool, failure: dict, palace_count: str) -> str:
    """Build a themed message for a Pre-compact mine event.

    Mining produces drawers across whatever wings the miner decides —
    we don't get to claim a target wing here. The message names the
    *source* directory (what we mined) rather than inventing a wing.
    """
    source = os.path.basename(mine_dir.rstrip("/")) or mine_dir
    if ok:
        msg = f"◈ Mined into the palace — source: {source}"
        if palace_count:
            msg += f", {palace_count}"
        return msg
    err = (failure or {}).get("error", "unknown error")
    return f"✘ Pre-compact mine failed — source: {source} — {err}"


def _theme_session_start(wing: str, response: dict) -> str:
    """One-line palace greeting at session start.

    Surfaces what mempalace already knows about the project this session
    is operating on. Calls ``tool_list_drawers(wing, room=sessions, limit=1)``
    to get a wing-scoped count regardless of which agent wrote each entry.
    (Was ``room=diary`` historically; renamed to match the canonical 7-room
    set per the Phase 1D FK migration on 2026-05-14.)

    Examples:
      ✦ palace ready — wing:familiar_realm_watch holds 47 diary entries
      ✦ palace ready — wing:new_project is a fresh wing
    """
    inner = {}
    try:
        content = response.get("result", {}).get("content", []) if isinstance(response, dict) else []
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        inner = {}

    display = _display_wing(wing)
    total = inner.get("total", 0) or 0

    if total == 0:
        return f"✦ palace ready — wing:{display} is a fresh wing"

    plural = "entry" if total == 1 else "entries"
    return f"✦ palace ready — wing:{display} holds {total:,} diary {plural}"


def _theme_precompact_save(wing: str, response: dict, palace_count: str) -> str:
    """Themed message for the pre-compact diary save (context boundary marker).

    Distinct from _theme_save_ok so the operator sees this is a boundary
    save (not a periodic checkpoint). Same chain shape, different sigil.
    """
    inner = {}
    try:
        content = response.get("result", {}).get("content", []) if isinstance(response, dict) else []
        if content and isinstance(content[0], dict):
            inner = json.loads(content[0].get("text", "{}"))
    except Exception:
        inner = {}
    topic = inner.get("topic", "precompact") or "precompact"
    timestamp = inner.get("timestamp", "") or ""
    drawer_label = _drawer_label(topic, timestamp)
    display = _display_wing(wing)
    chain = f"palace → wing:{display} → room:sessions → drawer:{drawer_label}"
    msg = f"◆ Pre-compact boundary save — {chain}"
    if palace_count:
        msg += f", palace now holds {palace_count}"
    return msg


def _desktop_notify(title: str, body: str) -> None:
    try:
        subprocess.Popen(
            ["notify-send", "--expire-time=4000", "--icon=dialog-information", title, body],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError):
        pass


def _parse_harness_input(data: dict, harness: str) -> dict:
    if harness not in SUPPORTED_HARNESSES:
        print(f"Unknown harness: {harness}", file=sys.stderr)
        sys.exit(1)
    return {
        "session_id": _sanitize_session_id(str(data.get("session_id", "unknown"))),
        "stop_hook_active": data.get("stop_hook_active", False),
        "transcript_path": str(data.get("transcript_path", "")),
    }


def _prune_state_files(max_age_days: int = 7):
    cutoff = time.time() - max_age_days * 86400
    try:
        for f in STATE_DIR.iterdir():
            if f.name in ("hook.log",):
                continue
            if f.suffix in ("", ) and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError:
        pass


def _mine_target_key(directory: str, mode: str, wing: str) -> str:
    """Stable hash for a (dir, mode, wing) mine target.

    Used as the filename of the per-target lock file so different targets
    get independent slots while the same (dir, mode, wing) triple collapses
    to a single slot — exactly the dedup semantics upstream's
    ``hooks_cli._pid_file_for_cmd`` provides.
    """
    raw = f"{directory}|{mode}|{wing}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _try_claim_mine_slot(directory: str, mode: str, wing: str):
    """Try to acquire an exclusive lock on the (dir, mode, wing) target.

    Returns the open file handle on success — the caller must keep it
    alive (i.e. unclosed) for the duration of the ``/mine`` POST so the
    lock stays held. Returns ``None`` if another hook process is already
    mining the same target.

    Uses ``fcntl.flock`` with ``LOCK_EX | LOCK_NB`` — fully atomic, no
    races between check and claim. Auto-releases when the holding process
    exits (even on crash), so stale locks from killed hooks never block.

    The dedup window is bounded by how long the holding hook holds /mine
    open: typically the request timeout (30s by default). After that the
    hook process exits, the lock releases, and the next save can fire
    another /mine. The daemon-side mine may still be running past that
    point — mempalace's miner dedupes by file via ``prefetch_mined_set``,
    so a duplicate fire just produces a "scan and skip" pass rather than
    a redundant mining of the same source files.
    """
    slot_dir = STATE_DIR / "mine_slots"
    try:
        slot_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    key = _mine_target_key(directory, mode, wing)
    slot_path = slot_dir / f"{key}.lock"
    try:
        fh = open(slot_path, "w")
    except OSError:
        return None
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        fh.close()
        return None
    # Stamp the slot for debugging — never read for logic.
    try:
        fh.write(f"pid={os.getpid()} dir={directory} mode={mode} wing={wing} ts={time.time()}\n")
        fh.flush()
    except OSError:
        pass
    return fh


def _ingest_transcript_via_daemon(daemon_url: str, transcript_path: str, wing: str):
    """Restore the transcript-ingest step that mempalace's upstream
    ``hooks_cli._ingest_transcript`` does on every save and precompact.

    Was dropped when palace-daemon's hook became a stdlib-only replacement
    for ``mempalace hook run`` (post-2026-05-11). Without this, Stop/PreCompact
    write only a marker diary entry — the verbatim conversation content
    that used to land via ``mempalace mine --mode convos`` was missing.

    Re-routed through the daemon's ``/mine`` endpoint so we don't take a
    Python import dependency on mempalace itself. Best-effort: any failure
    is logged and the save proceeds — never blocks the hook.
    """
    path = _validate_transcript_path(transcript_path)
    if path is None or not path.is_file() or path.stat().st_size < 100:
        return False
    settings = _load_hook_settings()
    if not settings.get("ingest_transcripts", True):
        return False

    mine_dir = str(path.parent)

    # Per-target dedup: if another hook process is currently inside /mine
    # for the same (dir, mode, wing), skip rather than queue a redundant
    # fire. The lock auto-releases when the holding hook exits.
    slot = _try_claim_mine_slot(mine_dir, "convos", wing)
    if slot is None:
        _log(f"Transcript ingest skipped (lock held): {path.name} wing={wing}")
        # Treat lock-held as success: another hook is already doing the
        # work; don't show a failure in themed output.
        return True

    ok = False
    try:
        ok, response = _post_mine(daemon_url, mine_dir,
                                  timeout=settings.get("mine_timeout_s", 60),
                                  mode="convos", wing=wing)
        if ok:
            _log(f"Transcript ingest queued: {path.name} → wing={wing}")
        else:
            err = (response or {}).get("error", "unknown")
            _log(f"Transcript ingest failed: {err}")
    except Exception as e:
        _log(f"Transcript ingest exception (non-fatal): {e}")
    finally:
        try:
            slot.close()
        except OSError:
            pass
    return ok


def hook_session_start(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]
    _log(f"SESSION START for session {session_id}")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _write_last_save_ts(session_id)   # seed so first-stop time_trigger doesn't fire immediately
    _prune_state_files()

    # Greet by surfacing palace state for the project this session is in.
    # Read is non-fatal: if the daemon is unreachable, fail silent rather
    # than block session startup with a transient error.
    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    wing = _project_wing(data, transcript_path)

    ok, response = _post_mcp(daemon_url, "mempalace_list_drawers", {
        "wing": wing,
        "room": "sessions",
        "limit": 1,
    })
    if ok:
        sys_msg = _theme_session_start(wing, response)
        _log(f"SESSION GREETING: {sys_msg}")
        _output({"systemMessage": sys_msg})
    else:
        _log(f"SESSION GREETING skipped (daemon unreachable for wing={wing})")
        _output({})


def hook_stop(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    stop_hook_active = parsed["stop_hook_active"]
    transcript_path = parsed["transcript_path"]

    if str(stop_hook_active).lower() in ("true", "1", "yes"):
        _output({})
        return

    exchange_count = _count_human_messages(transcript_path)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    last_save_file = STATE_DIR / f"{session_id}_last_save"
    last_save = 0
    if last_save_file.is_file():
        try:
            last_save = int(last_save_file.read_text().strip())
        except (ValueError, OSError):
            last_save = 0

    # Self-heal when the counter goes backward. Happens when the rules
    # for "what counts as a human message" tighten — e.g. the 2026-05-13
    # fix that stopped counting tool_result roundtrips dropped this
    # session's count from ~955 to ~87. Without this rebase, since_last
    # is negative, all three save triggers (count/time/force require
    # since_last > 0) wedge, and saves silently stop on running sessions.
    # Rebase the saved checkpoint to the current count: the next real
    # exchange resumes normal trigger behavior; one save cycle skipped.
    if last_save > exchange_count:
        _log(
            f"Session {session_id}: rebased stale last_save "
            f"({last_save} → {exchange_count}) — counter went backward, "
            f"likely after a count-rule change"
        )
        last_save = exchange_count
        try:
            last_save_file.write_text(str(exchange_count), encoding="utf-8")
        except OSError:
            pass

    since_last = exchange_count - last_save
    last_save_ts = _read_last_save_ts(session_id)
    time_since_last = time.time() - last_save_ts

    _log(f"Session {session_id}: {exchange_count} exchanges, {since_last} since last save, {time_since_last:.0f}s elapsed")

    settings = _load_hook_settings()

    # Three independent triggers — any one fires a save:
    #   count   — every SAVE_INTERVAL exchanges (existing behaviour)
    #   time    — every TIME_SAVE_INTERVAL seconds with unsaved exchanges (new)
    #   force   — force_on_stop=true: save at session end even with few exchanges,
    #             subject to FORCE_MIN_INTERVAL to prevent per-response spam
    count_trigger = since_last >= SAVE_INTERVAL and exchange_count > 0
    time_trigger  = time_since_last >= TIME_SAVE_INTERVAL and since_last > 0
    force_trigger = (
        settings.get("force_on_stop", True)
        and since_last > 0
        and time_since_last >= settings.get("force_min_interval", FORCE_MIN_INTERVAL)
    )

    if not (count_trigger or time_trigger or force_trigger):
        _output({})
        return

    trigger = "count" if count_trigger else ("time" if time_trigger else "force")
    _log(f"TRIGGERING SAVE at exchange {exchange_count} (trigger={trigger})")

    try:
        last_save_file.write_text(str(exchange_count), encoding="utf-8")
    except OSError:
        pass
    _write_last_save_ts(session_id)

    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    silent = settings.get("silent_save", True)
    toast = settings.get("desktop_toast", False)

    mine_dir = _get_mine_dir()
    if mine_dir:
        _log(f"Mine approval requested for {mine_dir}")
        if toast:
            _desktop_notify("MemPalace", f"Mine approval needed: {mine_dir}")
        _output({"decision": "block", "reason": _mine_approval_reason(mine_dir, daemon_url)})
        return

    if silent:
        # Phase 1D refactor (2026-05-14 hybrid-search-taxonomy spec, §3.5):
        # the hook no longer writes drawers directly via diary_write. The
        # miner is the single source of writes; the hook is a trigger.
        # /mine with mode=convos lets the miner chunk the transcript into
        # one-exchange-per-drawer + emits canonical-room values via the
        # post-spec detect_convo_room. The "single session summary"
        # semantic that diary_write previously provided is satisfied by
        # the convos-mode chunking + retrieval — the conversation is
        # captured as content-rich drawers, not as one opaque summary.
        wing = _project_wing(data, transcript_path)

        # Emit the themed systemMessage in the parent BEFORE detaching.
        # The detached child has stdout redirected to /dev/null (so
        # claude's harness can close its pipe and clear the event), which
        # means any _output call after detach is invisible to the user.
        # palace_count is the pre-save count — slightly stale but the
        # delta from one save is negligible at 273k drawers, and the
        # _get_palace_stats round-trip is ~50ms on a warm daemon.
        # Save success is optimistic: if the ingest later fails, the
        # failure path lands in hook.log only; the user sees the
        # success-themed line. The original (pre-detach) code emitted
        # only after the ingest returned, so the failure mode showed up
        # in the UI. We accept the slight loss of fidelity to recover
        # the in-session user-visible save confirmation that the detach
        # broke.
        pre_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
        sys_msg = _theme_save_ok(exchange_count, trigger, {}, pre_palace_count, wing)
        _output({"systemMessage": sys_msg})

        if not _detach_for_async_work():
            return

        # We are the (detached) child. Do the slow ingest. Anything
        # we print from here goes to /dev/null; logging via _log()
        # to ~/.mempalace/hook_state/hook.log is the durable channel.
        ok = _ingest_transcript_via_daemon(daemon_url, transcript_path, wing)
        _log(f"Silent save (mine-only) {'OK' if ok else 'FAILED (daemon unreachable)'} at exchange {exchange_count} → {wing}")
        if not ok:
            failure_themed = _theme_save_fail(exchange_count, trigger, {"error": "mine via daemon failed"})
            _log(f"FAILURE themed (would-have-emitted): {failure_themed}")
        if toast:
            _desktop_notify("MemPalace", f"Auto-saved at {exchange_count} msgs ({trigger})")
    else:
        if toast:
            _desktop_notify("MemPalace checkpoint", "Save requested — check Claude")
        _output({"decision": "block", "reason": STOP_BLOCK_REASON})


PRECOMPACT_TOPIC = "precompact"


def hook_precompact(data: dict, harness: str):
    parsed = _parse_harness_input(data, harness)
    session_id = parsed["session_id"]
    transcript_path = parsed["transcript_path"]
    _log(f"PRE-COMPACT triggered for session {session_id}")

    settings = _load_hook_settings()
    daemon_url = settings.get("daemon_url", "http://localhost:8085")
    toast = settings.get("desktop_toast", False)

    # Phase 1D refactor: precompact is also miner-only now. The transcript
    # ingest captures the current conversation state as drawers via the
    # miner; the previous "boundary marker" diary_write is dropped (it
    # was a stub anyway — the actual conversation content is what matters
    # at compaction, and the miner captures that).
    wing = _project_wing(data, transcript_path)

    # Emit themed systemMessage in the parent BEFORE detaching. See
    # hook_stop for the rationale. palace_count is pre-ingest; ingest
    # outcome is optimistic and falls back to hook.log on failure.
    # The MEMPAL_DIR mine's themed message (which depends on the mine
    # result) cannot be emitted in this single-fork architecture —
    # that path's outcome lands in hook.log only.
    pre_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
    sys_msgs = [_theme_precompact_save(wing, {}, pre_palace_count)]
    mine_dir = _get_mine_dir()
    if mine_dir:
        sys_msgs.append(f"⟳ Precompact MEMPAL_DIR mine queued: {mine_dir}")
    _output({"systemMessage": "\n".join(sys_msgs)})

    if not _detach_for_async_work():
        return

    # We are the (detached) child. Do the slow ingest + optional mine.
    # Both outcomes land in hook.log only; the user already saw the
    # optimistic themed line above.
    ingest_ok = _ingest_transcript_via_daemon(daemon_url, transcript_path, wing)
    _log(f"Pre-compact mine {'OK' if ingest_ok else 'FAILED'} → {wing}")
    if not ingest_ok:
        _log("FAILURE themed (would-have-emitted): ✘ Pre-compact transcript ingest failed — daemon unreachable")

    if mine_dir:
        _log(f"Precompact mine via daemon: {mine_dir}")
        ok, mine_response = _post_mine(daemon_url, mine_dir,
                                       timeout=60, mode="convos", wing=wing)
        _log(f"Precompact MEMPAL_DIR mine {'OK' if ok else 'skipped (daemon unreachable)'}")
        if ok:
            post_palace_count = _format_palace_count(_get_palace_stats(daemon_url))
            _log(f"MINE-RESULT themed (would-have-emitted): {_theme_mine(mine_dir, ok, mine_response, post_palace_count)}")
        else:
            _log(f"MINE-RESULT themed (would-have-emitted): {_theme_mine(mine_dir, ok, mine_response, '')}")
    else:
        _log("Precompact MEMPAL_DIR mine skipped: MEMPAL_DIR not set")

    if toast:
        _desktop_notify("MemPalace", "Pre-compaction checkpoint triggered")


def run_hook(hook_name: str, harness: str):
    # Read stdin in the parent so the child has it whether or not we detach.
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, EOFError):
        _log("WARNING: Failed to parse stdin JSON, proceeding with empty data")
        data = {}

    # Stop/precompact do slow HTTP round-trips to palace-daemon and
    # would block the harness — but the detach is now handled inside
    # each handler (after it emits its user-visible systemMessage via
    # _output) by calling _detach_for_async_work(). The dispatcher
    # just dispatches; the handlers decide when to fork.
    hooks = {
        "session-start": hook_session_start,
        "stop": hook_stop,
        "precompact": hook_precompact,
    }

    handler = hooks.get(hook_name)
    if handler is None:
        print(f"Unknown hook: {hook_name}", file=sys.stderr)
        sys.exit(1)

    handler(data, harness)


def main():
    parser = argparse.ArgumentParser(description="palace-daemon hook runner")
    parser.add_argument("--hook", required=True, choices=["session-start", "stop", "precompact"])
    parser.add_argument("--harness", required=True)
    args = parser.parse_args()
    run_hook(args.hook, args.harness)


if __name__ == "__main__":
    main()
