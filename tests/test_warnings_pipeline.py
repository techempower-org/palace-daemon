"""Tests for the mempalace#86 warnings/errors pipeline.

Covers three slices of the change:

1. ``messages.ensure_warnings_fields`` — daemon-side response normalization.
   The HTTP routes (/memory, /silent-save) call this on every response so
   clients can rely on ``warnings`` / ``errors`` being present even when
   paired with an older mempalace that doesn't emit them.

2. ``messages.save_ok`` — themed systemMessage rendering for /silent-save.
   Glyph + verb reflect the actual outcome (clean / warn / fail).

3. ``clients/hook.py`` themed-chain renderers — same outcome surfacing on
   the hook-emitted "◆ palace → wing:... → room:... → drawer:..." lines.

Run with::

    cd /home/jp/Projects/palace-daemon
    python -m unittest tests.test_warnings_pipeline -v
"""
import json
import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_CLIENTS = os.path.join(_ROOT, "clients")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _CLIENTS not in sys.path:
    sys.path.insert(0, _CLIENTS)

import messages  # noqa: E402  (daemon-root module, hnswlib-free)
import hook      # noqa: E402  (clients/hook.py)


# ── Daemon-side: response normalization ──────────────────────────────────────

class TestEnsureWarningsFields(unittest.TestCase):
    """The shape-normalizer that /memory and /silent-save both call.

    Guarantees the response always carries ``warnings`` and ``errors`` lists
    so clients can ``response.get("warnings", [])`` without conditional
    handling for the stock-mempalace case.
    """

    def test_empty_arrays_when_mempalace_omits_fields(self):
        # Stock / older mempalace: no warnings field on the write response.
        result = messages.ensure_warnings_fields({"success": True, "entry_id": "drawer_abc"})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["entry_id"], "drawer_abc")

    def test_passes_warnings_through_when_mempalace_emits_them(self):
        result = messages.ensure_warnings_fields({
            "success": True,
            "entry_id": "drawer_abc",
            "warnings": ["room 'diary' is not canonical. accepted as-is."],
        })
        self.assertEqual(
            result["warnings"],
            ["room 'diary' is not canonical. accepted as-is."],
        )
        self.assertEqual(result["errors"], [])

    def test_passes_errors_through(self):
        result = messages.ensure_warnings_fields({
            "success": False,
            "errors": ["HNSW index rebuilding, write rejected"],
        })
        self.assertEqual(
            result["errors"],
            ["HNSW index rebuilding, write rejected"],
        )

    def test_coerces_non_string_items_defensively(self):
        # A misbehaving mempalace could put a non-string in the list.
        # We coerce to str so downstream rendering can't crash.
        result = messages.ensure_warnings_fields({
            "warnings": [42, {"oops": True}, "real warning"],
        })
        self.assertEqual(len(result["warnings"]), 3)
        for item in result["warnings"]:
            self.assertIsInstance(item, str)

    def test_replaces_invalid_field_type_with_empty_list(self):
        # warnings: "some string" rather than a list — treat as empty.
        result = messages.ensure_warnings_fields({"warnings": "not-a-list"})
        self.assertEqual(result["warnings"], [])

    def test_non_dict_payload_passes_through(self):
        # JSON-RPC error envelopes etc — don't mangle them.
        self.assertEqual(messages.ensure_warnings_fields(None), None)
        self.assertEqual(messages.ensure_warnings_fields("oops"), "oops")
        self.assertEqual(messages.ensure_warnings_fields([1, 2]), [1, 2])


# ── Daemon-side: themed systemMessage rendering ──────────────────────────────

class TestSaveOkSystemMessage(unittest.TestCase):
    """``messages.save_ok`` is the systemMessage body for /silent-save.

    The leading glyph reflects outcome: ✦ (clean) / ⚠ (warning) / ✕ (failed).
    Warnings and errors render on an indented second line so they're easy
    to spot inside the existing "◆ palace → ..." chain shape.
    """

    def test_clean_path_uses_legacy_phrasing(self):
        # No warnings → unchanged from before mempalace#86. Operators
        # see the existing "memory woven" voice on the healthy path.
        msg = messages.save_ok(1, themes=())
        self.assertTrue(msg.startswith("✦"))
        self.assertIn("woven into the palace", msg)

    def test_warning_changes_glyph_and_appends_note(self):
        msg = messages.save_ok(
            1,
            themes=(),
            warnings=["room 'diary' is not canonical. accepted as-is."],
        )
        self.assertTrue(msg.startswith("⚠"))
        self.assertIn("Saved with warning", msg)
        # Indented secondary line carries the actual warning text.
        self.assertIn("room 'diary' is not canonical", msg)
        self.assertIn("\n    ", msg)

    def test_error_changes_glyph_and_signals_failure(self):
        msg = messages.save_ok(
            3,
            themes=(),
            errors=["HNSW index rebuilding, write rejected"],
        )
        self.assertTrue(msg.startswith("✕"))
        self.assertIn("Save FAILED", msg)
        self.assertIn("HNSW index rebuilding", msg)

    def test_errors_take_priority_over_warnings(self):
        # If both surfaced, the failure must dominate — operator should
        # see this as a failure, not a successful-but-noisy save.
        msg = messages.save_ok(
            1,
            warnings=["non-canonical room"],
            errors=["write rejected"],
        )
        self.assertTrue(msg.startswith("✕"))
        self.assertIn("write rejected", msg)


# ── Hook side: themed-chain rendering ────────────────────────────────────────

def _make_response(*, entry_id="diary_x_2026_abc", topic="checkpoint",
                   timestamp="2026-05-15T08:48:16.427801",
                   warnings=None, errors=None):
    """Shape the MCP-wrapped response that hook.py parses.

    Mempalace returns the inner dict JSON-encoded inside ``content[0].text``.
    mempalace#86 adds optional warnings/errors lists to that inner dict.
    """
    inner = {
        "success": not errors,
        "entry_id": entry_id,
        "agent": "claude-code",
        "topic": topic,
        "timestamp": timestamp,
    }
    if warnings is not None:
        inner["warnings"] = warnings
    if errors is not None:
        inner["errors"] = errors
    return {
        "result": {"content": [{"type": "text", "text": json.dumps(inner)}]}
    }


class TestThemedChainClean(unittest.TestCase):
    """Clean save — no warnings, no errors → unchanged from before #86."""

    def test_uses_clean_glyph(self):
        msg = hook._theme_save_ok(
            exchange_count=42,
            trigger="count",
            response=_make_response(),
            palace_count="183,000 drawers",
            wing="familiar_realm_watch",
        )
        # ✦ is the established clean-path glyph. Must not regress.
        self.assertIn("✦", msg)
        self.assertNotIn("⚠", msg)
        self.assertNotIn("✕", msg)
        self.assertIn("wing:familiar_realm_watch", msg)
        self.assertIn("room:sessions", msg)


class TestThemedChainWarning(unittest.TestCase):
    """Non-canonical room / deprecated topic / similar → warn glyph + note."""

    def test_renders_warning_glyph_and_indented_note(self):
        msg = hook._theme_save_ok(
            exchange_count=42,
            trigger="count",
            response=_make_response(warnings=[
                "room 'diary' is not canonical (canonical: sessions). accepted as-is.",
            ]),
            palace_count="183,000 drawers",
            wing="familiar_realm_watch",
        )
        # Leading glyph reflects the warning.
        self.assertIn("⚠", msg)
        self.assertIn("Saved with warning", msg)
        # Chain still rendered — same shape as the clean path.
        self.assertIn("palace → wing:familiar_realm_watch", msg)
        self.assertIn("room:sessions", msg)
        # Indented secondary line carries the warning text verbatim.
        self.assertIn("room 'diary' is not canonical", msg)
        self.assertIn("\n    ", msg)

    def test_handles_multiple_warnings(self):
        msg = hook._theme_save_ok(
            exchange_count=1, trigger="force",
            response=_make_response(warnings=["one", "two", "three"]),
            palace_count="",
            wing="test",
        )
        self.assertIn("Saved with warnings", msg)  # plural
        self.assertIn("one", msg)
        self.assertIn("two", msg)
        self.assertIn("three", msg)


class TestThemedChainError(unittest.TestCase):
    """Write actually rejected → fail glyph + error message."""

    def test_renders_error_glyph_and_failure_phrasing(self):
        msg = hook._theme_save_ok(
            exchange_count=42,
            trigger="count",
            response=_make_response(errors=[
                "HNSW index rebuilding, write rejected",
            ]),
            palace_count="",
            wing="palace_daemon",
        )
        self.assertIn("✕", msg)
        self.assertIn("Save FAILED", msg)
        self.assertIn("HNSW index rebuilding", msg)
        # Chain still surfaces so the operator knows which wing was targeted.
        self.assertIn("wing:palace_daemon", msg)

    def test_error_dominates_when_both_present(self):
        msg = hook._theme_save_ok(
            exchange_count=1,
            trigger="force",
            response=_make_response(
                warnings=["non-canonical room"],
                errors=["write rejected during rebuild"],
            ),
            palace_count="",
            wing="x",
        )
        self.assertIn("✕", msg)
        self.assertNotIn("⚠", msg)
        self.assertIn("write rejected during rebuild", msg)


class TestThemedPrecompactWithOutcome(unittest.TestCase):
    """Pre-compact save mirrors the save-ok outcome treatment."""

    def test_clean_precompact_uses_legacy_diamond(self):
        msg = hook._theme_precompact_save(
            wing="familiar_realm_watch",
            response=_make_response(topic="precompact"),
            palace_count="183,500 drawers",
        )
        self.assertIn("◆", msg)
        self.assertNotIn("⚠", msg)
        self.assertNotIn("✕", msg)

    def test_warning_surfaces_on_precompact(self):
        msg = hook._theme_precompact_save(
            wing="familiar_realm_watch",
            response=_make_response(
                topic="precompact",
                warnings=["room 'diary' is not canonical. accepted as-is."],
            ),
            palace_count="",
        )
        self.assertIn("⚠", msg)
        self.assertIn("room 'diary' is not canonical", msg)
        self.assertIn("wing:familiar_realm_watch", msg)


class TestFailMessageWithErrorsArray(unittest.TestCase):
    """_theme_save_fail accepts the new ``errors`` array shape too."""

    def test_renders_errors_list(self):
        msg = hook._theme_save_fail(
            exchange_count=99, trigger="count",
            failure={"errors": ["HNSW rebuilding", "queue full"]},
        )
        self.assertIn("99", msg)
        self.assertIn("HNSW rebuilding", msg)
        self.assertIn("queue full", msg)

    def test_legacy_error_string_still_works(self):
        # Transport-level failures still arrive as {"error": "..."}, not as
        # an errors array. The renderer must accept both shapes.
        msg = hook._theme_save_fail(
            exchange_count=10, trigger="time",
            failure={"error": "HTTP 401 Unauthorized"},
        )
        self.assertIn("401", msg)


class TestOutcomeHelpers(unittest.TestCase):
    """Internal helpers — sanity-check before they get composed elsewhere."""

    def test_split_outcome_defaults_to_empty(self):
        warnings, errors = hook._split_outcome({})
        self.assertEqual(warnings, [])
        self.assertEqual(errors, [])

    def test_split_outcome_coerces_to_str(self):
        warnings, errors = hook._split_outcome({"warnings": [1, 2], "errors": [None]})
        self.assertEqual(warnings, ["1", "2"])
        self.assertEqual(errors, ["None"])

    def test_split_outcome_rejects_non_list(self):
        warnings, errors = hook._split_outcome({"warnings": "oops", "errors": 99})
        self.assertEqual(warnings, [])
        self.assertEqual(errors, [])

    def test_extract_inner_handles_already_unwrapped(self):
        # /memory and /silent-save return already-unwrapped dicts; the
        # themed renderer must accept that shape too, not only the
        # JSON-RPC envelope form.
        inner = hook._extract_inner({
            "success": True,
            "entry_id": "drawer_x",
            "warnings": ["w"],
            "errors": [],
        })
        self.assertEqual(inner["entry_id"], "drawer_x")
        self.assertEqual(inner["warnings"], ["w"])


if __name__ == "__main__":
    unittest.main()
