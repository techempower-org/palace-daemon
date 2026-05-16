"""
Themed user-facing strings for palace-daemon.

Keep the voice consistent across save paths, repair operations, and drain
events. One file, one place to retheme everything.

Glyphs:
  ✦ — a memory operation (save, drain, held-in-trust)
  ⚠ — a memory operation with a non-fatal warning (e.g. non-canonical room)
  ✕ — a memory operation that failed
  ◈ — a palace operation (repair, reload, backup, restore)
"""

from typing import Iterable


def _theme_tag(themes: Iterable[str]) -> str:
    items = [t for t in (themes or []) if t]
    if not items:
        return ""
    return " — " + ", ".join(items[:4])


def _format_notes(notes: Iterable[str]) -> str:
    """Render warning/error notes as an indented secondary line.

    Empty inputs return an empty string — the caller can append unconditionally.
    """
    items = [str(n).strip() for n in (notes or []) if str(n).strip()]
    if not items:
        return ""
    return "\n    " + "\n    ".join(items)


def ensure_warnings_fields(payload):
    """Normalize a write-path response so it always carries ``warnings`` and
    ``errors`` lists (mempalace#86).

    Newer mempalace versions include ``warnings: list[str]`` (and optionally
    ``errors: list[str]``) on drawer-write responses. We forward those fields
    unchanged, but when paired with an older mempalace that doesn't emit
    them, we still return the keys with empty lists so callers can rely on
    the shape.

    Non-dict payloads pass through untouched — they're either error envelopes
    or already-shaped objects that the caller will handle on its own.
    """
    if not isinstance(payload, dict):
        return payload
    warnings = payload.get("warnings")
    if not isinstance(warnings, list):
        warnings = []
    errors = payload.get("errors")
    if not isinstance(errors, list):
        errors = []
    payload["warnings"] = [str(w) for w in warnings]
    payload["errors"] = [str(e) for e in errors]
    return payload


def save_ok(
    count: int,
    themes: Iterable[str] = (),
    warnings: Iterable[str] = (),
    errors: Iterable[str] = (),
) -> str:
    """Silent-save outcome line — glyph + body + optional notes.

    mempalace#86: when the underlying write returned warnings (e.g. a
    non-canonical room was accepted as-is) or errors (e.g. HNSW rebuilding,
    write rejected), surface them on an indented second line so the user
    sees what actually happened, not just what was attempted.
    """
    warnings = list(warnings or [])
    errors = list(errors or [])
    if errors:
        glyph = "✕"
        verb = "Save FAILED"
    elif warnings:
        glyph = "⚠"
        verb = "Saved with warning" if len(warnings) == 1 else "Saved with warnings"
    else:
        glyph = "✦"
        verb = None  # legacy "memory woven" phrasing below

    if verb is None:
        # Backwards-compatible phrasing when no warnings/errors are present.
        # Keeps the existing themed voice for the healthy path.
        if count == 1:
            head = f"{glyph} 1 memory woven into the palace{_theme_tag(themes)}"
        else:
            head = f"{glyph} {count} memories woven into the palace{_theme_tag(themes)}"
        return head

    if count == 1:
        head = f"{glyph} {verb} — 1 memory{_theme_tag(themes)}"
    else:
        head = f"{glyph} {verb} — {count} memories{_theme_tag(themes)}"
    return head + _format_notes(errors or warnings)


def save_queued(count: int, themes: Iterable[str] = ()) -> str:
    """Silent-save deferred because repair is underway."""
    if count == 1:
        return (
            f"✦ 1 memory held in trust{_theme_tag(themes)} "
            f"— the palace is being mended"
        )
    return (
        f"✦ {count} memories held in trust{_theme_tag(themes)} "
        f"— the palace is being mended"
    )


def repair_begin(mode: str) -> str:
    if mode == "rebuild":
        return (
            "◈ Mending begun — the halls are quieted while the index is rebuilt"
        )
    if mode == "prune":
        return "◈ Pruning begun — corrupted threads are being cleared"
    if mode == "scan":
        return "◈ Scanning begun — the walls are being read"
    return "◈ Light maintenance — stale segments are being set aside"


def repair_complete(mode: str, drained: int = 0, duration_s: float = 0.0) -> str:
    dur = f" in {duration_s:.1f}s" if duration_s else ""
    if mode == "rebuild":
        if drained:
            verb = "memory flowed" if drained == 1 else "memories flowed"
            return (
                f"◈ The palace is whole again{dur} "
                f"— {drained} held {verb} home"
            )
        return f"◈ The palace is whole again{dur}"
    if mode == "prune":
        return f"◈ Pruning complete{dur}"
    if mode == "scan":
        return f"◈ Scan complete{dur}"
    return f"◈ Maintenance complete{dur}"


def drain_fail(count: int) -> str:
    if count == 1:
        return "✦ 1 held memory could not be placed — kept in the antechamber"
    return (
        f"✦ {count} held memories could not be placed "
        "— kept in the antechamber"
    )
