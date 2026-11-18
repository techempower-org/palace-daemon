"""The palace lane's checks (scripts/checks): one line each, judged from fictional fixtures."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import io
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

CHECKS = Path(__file__).resolve().parent.parent / "scripts" / "checks"
sys.path.insert(0, str(CHECKS))


def _load(name):
    spec = importlib.util.spec_from_file_location(
        f"check_{name}", CHECKS / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


checklib = _load("checklib")
dream = _load("dream_night")
secrets = _load("secret_drawers")
gs = _load("gs_auth")
needles = _load("redact_needles")
psec = _load("palace_secrets")
pclass = _load("palace_secrets_classify")
pgt = _load("pg_timeouts")

NOW = dt.datetime(2026, 11, 15, 9, 0, tzinfo=dt.timezone(dt.timedelta(hours=-8)))
SHAPE = r"^CHECK [a-z-]+ (ok|warn|fail) · .+ · .+$"


def _night(day, hours_ago, con):
    made = (NOW - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {
        "day": day,
        "generated_at": made,
        "run": {"steps": {"contradictions": con}},
        "proposals": [{}] * 3,
    }


OFF = {
    "pairs": 60,
    "judged": 0,
    "error": None,
    "skipped": True,
    "judge": "off",
    "waiting": 60,
    "cached": 0,
}
GOOD = {
    "pairs": 60,
    "judged": 0,
    "error": None,
    "cached": 60,
    "verdicts": {"unrelated": 60},
}
RAN = {
    "LoadState": "loaded",
    "Result": "success",
    "ExecMainStatus": "0",
    "ExecMainExitTimestamp": "Sun 2026-11-15 03:31:40 PST",
}


def _host(
    required=True,
    events=(),
    bwfix=1,
    active="active",
    tokenconf=True,
    journal=True,
    now=10_000.0,
):
    return {
        "active": active,
        "status": {"auth": {"required": required}},
        "events": list(events),
        "bwfix": bwfix,
        "tokenconf": tokenconf,
        "journal": journal,
        "now": now,
    }


class PalaceChecks(unittest.TestCase):
    """Run by unittest (as CI does) and by pytest."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="palace-checks-test."))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def env(self, **values):
        return mock.patch.dict(os.environ, values)

    def stdout(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert fn() in (0, None)
        return buf.getvalue()

    # --- checklib

    def test_a_line_has_the_fixed_shape_and_refuses_an_unknown_state(self):
        import re

        text = checklib.line("x-y", "warn", "3 things at 09:00")
        assert re.match(SHAPE, text)
        with self.assertRaises(ValueError):
            checklib.line("x", "bad", "f")
        with self.env(PALACE_CHECKS_DIR=str(self.tmp / "c")):
            checklib.emit("x-y", text)
        assert (self.tmp / "c" / "x-y.line").read_text() == text + "\n"

    # --- dream-night

    def test_dream_ok_when_the_night_published_and_the_judge_answered(self):
        out = dream.assess([_night("2026-11-15", 5, GOOD)], RAN, NOW)
        assert (
            out.startswith("CHECK palace-dream-night ok")
            and "exit 0" in out
            and "60 cached" in out
        )

    def test_dream_fails_when_nothing_was_published_or_the_night_is_stale(self):
        assert dream.assess([], RAN, NOW).startswith("CHECK palace-dream-night fail")
        stale = dream.assess([_night("2026-11-13", 30, GOOD)], RAN, NOW)
        assert (
            stale.startswith("CHECK palace-dream-night fail") and "older than" in stale
        )

    def test_dream_warns_on_a_bad_exit_or_a_judge_error(self):
        bad = dream.assess(
            [_night("2026-11-15", 5, GOOD)], {**RAN, "ExecMainStatus": "3"}, NOW
        )
        assert (
            bad.startswith("CHECK palace-dream-night warn")
            and "exit 3" in bad
            and "last run" in bad
        )
        err = dream.assess(
            [_night("2026-11-15", 5, {**GOOD, "error": "TimeoutError: busy"})], RAN, NOW
        )
        assert err.startswith("CHECK palace-dream-night warn") and "judge error" in err

    def test_dream_warns_when_the_timer_start_was_skipped_after_the_newest_night(self):
        # 2026-11-16 and 11-17: the unit's ExecCondition (a MemAvailable floor) skipped the
        # 03:30 start. A skip leaves no exit timestamp, so the line read "ok ... no run since
        # boot" for a day and only failed once the night went stale.
        skip = {
            "LoadState": "loaded",
            "Result": "exec-condition",
            "ExecMainStatus": "0",
            "ExecMainExitTimestamp": "",
            "ConditionTimestamp": f"@{int((NOW - dt.timedelta(hours=5)).timestamp())}",
        }
        out = dream.assess([_night("2026-11-14", 25, GOOD)], skip, NOW)
        assert out.startswith("CHECK palace-dream-night warn"), out
        assert (
            "skipped by its start condition" in out and "no run since boot" not in out
        )
        # a night published by hand after the skip: ok, and the line still names the skip
        later = dream.assess([_night("2026-11-15", 1, GOOD)], skip, NOW)
        assert later.startswith("CHECK palace-dream-night ok"), later
        assert "skipped by its start condition" in later

    def test_dream_skip_edges_from_review(self):
        # the live 11-17 systemctl show --timestamp=unix output after the skip
        live = dream.unit_result(
            "LoadState=loaded\nResult=exec-condition\nExecMainStatus=0\n"
            "ExecMainExitTimestamp=\nConditionTimestamp=@1794915039\n"
            "InactiveEnterTimestamp=@1794915039\n"
        )
        assert dream.skipped_at(live) == dt.datetime(
            2026, 11, 17, 11, 30, 39, tzinfo=dt.timezone.utc
        )
        at = (NOW - dt.timedelta(hours=5)).replace(microsecond=0)
        stamp = f"@{int(at.timestamp())}"
        base = {
            "LoadState": "loaded",
            "Result": "exec-condition",
            "ExecMainStatus": "0",
        }
        # an exit timestamp kept from an earlier run, and a night published after the skip: ok
        kept = {**base, "ExecMainExitTimestamp": "@1", "ConditionTimestamp": stamp}
        ok = dream.assess([_night("2026-11-15", 1, GOOD)], kept, NOW)
        assert ok.startswith("CHECK palace-dream-night ok"), ok
        # a night published at the very second of the skip is not newer: warn
        same = dream.assess([_night("2026-11-15", 5, GOOD)], kept, NOW)
        assert same.startswith("CHECK palace-dream-night warn"), same
        # ConditionTimestamp empty: InactiveEnterTimestamp is used
        inactive = {
            **base,
            "ExecMainExitTimestamp": "",
            "ConditionTimestamp": "",
            "InactiveEnterTimestamp": stamp,
        }
        assert dream.skipped_at(inactive) == at.astimezone(dt.timezone.utc)
        # an out-of-range ConditionTimestamp falls back too
        huge = {**inactive, "ConditionTimestamp": "@999999999999"}
        assert dream.skipped_at(huge) == at.astimezone(dt.timezone.utc)
        # a skip with no readable time: warn, never ok
        untimed = {**base, "ExecMainExitTimestamp": "", "ConditionTimestamp": "n/a"}
        out = dream.assess([_night("2026-11-15", 1, GOOD)], untimed, NOW)
        assert out.startswith("CHECK palace-dream-night warn"), out
        assert "time unreadable" in out

    def test_dream_still_says_no_run_since_boot_when_the_unit_never_started(self):
        never = {
            "LoadState": "loaded",
            "Result": "success",
            "ExecMainStatus": "0",
            "ExecMainExitTimestamp": "",
            "ConditionTimestamp": "",
        }
        out = dream.assess([_night("2026-11-15", 5, GOOD)], never, NOW)
        assert (
            out.startswith("CHECK palace-dream-night ok") and "no run since boot" in out
        )

    def test_dream_warns_only_after_three_nights_with_the_judge_off(self):
        two = [
            _night("2026-11-15", 5, OFF),
            _night("2026-11-14", 29, OFF),
            _night("2026-11-13", 53, GOOD),
        ]
        assert dream.assess(two, RAN, NOW).startswith("CHECK palace-dream-night ok")
        three = [
            _night("2026-11-15", 5, OFF),
            _night("2026-11-14", 29, OFF),
            _night("2026-11-13", 53, OFF),
        ]
        out = dream.assess(three, RAN, NOW)
        assert (
            out.startswith("CHECK palace-dream-night warn")
            and "judge off 3 nights" in out
        )

    def test_dream_reads_only_day_files_newest_first(self):
        for day in ("2026-11-13", "2026-11-15", "2026-11-14"):
            (self.tmp / f"{day}.json").write_text(json.dumps({"day": day}))
        (self.tmp / "latest.json").write_text("{}")
        (self.tmp / "2026-11-16.json").write_text("{broken")
        nights = dream.read_nights(self.tmp)
        assert nights[0] is dream.UNREADABLE  # the corrupt newest is kept, not skipped
        assert [n["day"] for n in nights[1:]] == ["2026-11-15", "2026-11-14"]
        out = dream.assess(nights, RAN, NOW)
        assert out.startswith(
            "CHECK palace-dream-night fail · the newest published night is unreadable"
        )

    # --- secret-drawers

    def test_a_hash_is_new_only_when_its_first_drawer_anywhere_is_in_the_window(self):
        since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
        rows = [
            ("aws", "aaaa0000", "w1", "2026-11-14T10:00:00Z"),  # new
            (
                "aws",
                "aaaa0000",
                "w2",
                "2026-11-14T11:00:00Z",
            ),  # same secret, a second wing
            ("jwt", "bbbb1111", "w1", "2026-11-14T10:00:00Z"),  # seen again today ...
            (
                "jwt",
                "bbbb1111",
                "w3",
                "2026-10-01T10:00:00Z",
            ),  # ... but first held in October
        ]
        assert secrets.new_secrets(rows, since) == [("aws", ["w1", "w2"])]

    def test_raw_text_after_the_rules_loaded_fails_and_older_is_a_sweep_candidate(self):
        loaded = dt.datetime(2026, 11, 14, 16, 0, tzinfo=dt.timezone.utc)

        def redact(doc):
            return doc.replace("SECRET", "[redacted litellm sha256:00000000]")

        rows = [
            ("w1", "2026-11-14T17:00:00Z", "a SECRET here"),
            ("w2", "2026-11-14T15:00:00Z", "an older SECRET"),
            (
                "w3",
                "2026-11-14T18:00:00Z",
                "[redacted assignment] and [redacted code], clean",
            ),
        ]
        raw, older, unhashed, n = secrets.scan_window(rows, redact, loaded)
        assert raw == {("w1", "litellm"): 1} and older == {("w2", "litellm"): 1}
        assert unhashed == 2 and n == 3

    def test_secret_lines_fail_warn_and_ok_never_print_a_value(self):
        since = NOW
        fail = secrets.assess([], {("w1", "aws"): 2}, {}, 0, 10, since)
        assert (
            fail.startswith("CHECK palace-secret-drawers fail") and "w1 (aws×2)" in fail
        )
        warn = secrets.assess(
            [("jwt", ["w2", "w3"])], {}, {("w4", "litellm"): 1}, 5, 10, since
        )
        assert warn.startswith("CHECK palace-secret-drawers warn")
        assert (
            "1 secret(s) first mined" in warn
            and "w2 (jwt×1)" in warn
            and "w4 (litellm×1)" in warn
        )
        ok = secrets.assess([], {}, {}, 7, 10, since)
        assert ok.startswith("CHECK palace-secret-drawers ok") and "7 unhashed" in ok

    def test_filed_reads_utc_and_naive_local_stamps(self):
        assert secrets.filed("2026-11-14T15:17:39.103Z").utcoffset() == dt.timedelta(0)
        assert secrets.filed("2026-11-14T08:05:45.109123").tzinfo is not None
        assert secrets.filed("") is None and secrets.filed("nope") is None

    # --- gs-auth

    def test_refusals_right_after_a_start_are_deploy_probes_not_callers(self):
        ev = [
            [100.0, "start"],
            [100.5, "refused"],
            [130.0, "refused"],
            [500.0, "refused"],
        ]
        callers, probes, last = gs.caller_refusals(ev)
        assert (callers, probes, last) == (1, 2, 500.0)
        assert gs.caller_refusals([[5.0, "refused"]])[:2] == (
            1,
            0,
        )  # no start in the window

    def test_gs_ok_when_each_host_is_as_expected(self):
        out = gs.assess(
            [
                ("h1", "required", _host(True, [[1.0, "start"], [2.0, "refused"]])),
                ("h2", "open", _host(False)),
            ],
            NOW,
        )
        assert out.startswith("CHECK palace-gs-auth ok") and "1 deploy probe(s)" in out

    def test_gs_warns_on_a_refused_caller_a_wrong_state_or_an_old_service(self):
        caller = gs.assess([("h1", "required", _host(True, [[900.0, "refused"]]))], NOW)
        assert (
            caller.startswith("CHECK palace-gs-auth warn")
            and "a caller is refused" in caller
        )
        flipped = gs.assess([("h1", "required", _host(False))], NOW)
        assert (
            flipped.startswith("CHECK palace-gs-auth warn")
            and "expected required" in flipped
        )
        old = gs.assess([("h2", "open", _host(False, bwfix=0))], NOW)
        assert old.startswith("CHECK palace-gs-auth warn") and "boot-PATH" in old

    def test_gs_fails_when_a_host_is_down_unreachable_or_unreadable(self):
        assert gs.assess([("h1", "open", None)], NOW).startswith(
            "CHECK palace-gs-auth fail"
        )
        assert gs.assess([("h1", "open", _host(active="failed"))], NOW).startswith(
            "CHECK palace-gs-auth fail"
        )
        unreadable = {"active": "active", "status": None, "events": [], "bwfix": 1}
        assert gs.assess([("h1", "open", unreadable)], NOW).startswith(
            "CHECK palace-gs-auth fail"
        )
        assert gs.assess([], NOW).startswith("CHECK palace-gs-auth fail")

    def test_the_worst_host_decides_and_every_host_is_named(self):
        out = gs.assess([("h1", "open", _host(False)), ("h2", "open", None)], NOW)
        assert (
            out.startswith("CHECK palace-gs-auth fail")
            and "h1 open" in out
            and "h2 unreachable" in out
        )

    def test_host_specs_are_parsed_and_bad_items_reported(self):
        hosts, bad = gs.expectations(
            "a=required b=open@desk-b c=maybe =open d e=open@-x f=open@a"
        )
        assert hosts == [("a", "required", "desktop-1"), ("b", "open", "desk-b")]
        assert bad == [3, 4, 5, 6, 7]  # f's label is a host name: refused
        hosts, bad = gs.expectations(
            "desktop-1=required"
        )  # the default label would be the host
        assert hosts == [] and bad == [1]

    def test_no_host_name_or_private_address_is_written_into_the_checks(self):
        # The names come from the local config (never listed here: this repo is public); CI has none,
        # so it checks the address shapes only.
        import os
        import re

        env = Path(os.path.expanduser("~/.config/palace-checks/env"))
        names = set()
        if env.exists():
            for row in env.read_text().splitlines():
                if row.startswith("GS_CHECK_HOSTS="):
                    names |= {
                        h
                        for h, _, _ in gs.expectations(row.split("=", 1)[1].strip('"'))[
                            0
                        ]
                    }
                if row.startswith(("DREAM_OUT=", "PALACE_CHECKS_DIR=")):
                    names.add(row.split("=", 1)[1].strip('"'))
        private = re.compile(r"\b(?:10|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d+\.\d+")
        repo = CHECKS.parent.parent
        files = (
            list(CHECKS.glob("*.py"))
            + list((CHECKS.parent / "systemd").glob("*"))
            + [repo / "docs" / "CHECKS.md", Path(__file__)]
        )
        for f in files:
            text = f.read_text()
            assert not private.search(text), f"{f.name} holds a private address"
            for name in names:
                assert name not in text, f"{f.name} names a local host or path"

    def test_dream_fails_cleanly_on_an_unreadable_night_or_a_missing_dir(self):
        bad = dream.assess([{"day": "2026-11-15", "run": {}}], RAN, NOW)
        assert bad.startswith("CHECK palace-dream-night fail") and "generated_at" in bad
        with self.env(
            PALACE_CHECKS_DIR=str(self.tmp / "c"), DREAM_OUT=str(self.tmp / "nowhere")
        ):
            out = self.stdout(dream.main)
        assert out.startswith(
            "CHECK palace-dream-night fail · DREAM_OUT is not a directory"
        )

    # --- review round 1 (xreview): fail closed, never tokenless, never leak

    def test_gs_never_probes_without_a_token_config_and_says_so(self):
        out = gs.assess([("h1", "required", _host(tokenconf=False))], NOW)
        assert out.startswith("CHECK palace-gs-auth fail") and "never tokenless" in out

    def test_gs_warns_when_the_journal_could_not_be_read(self):
        out = gs.assess([("h1", "required", _host(journal=False))], NOW)
        assert (
            out.startswith("CHECK palace-gs-auth warn") and "journal unreadable" in out
        )

    def test_refusals_before_the_window_are_dropped_but_their_start_still_matches_a_probe(
        self,
    ):
        day = 24 * 3600
        now = 200_000.0
        start = now - day
        ev = [[start - 30, "start"], [start + 10, "refused"], [start - 5, "refused"]]
        assert gs.caller_refusals(ev, start) == (0, 1, None)

    def test_host_specs_reject_ssh_options_and_odd_names(self):
        hosts, bad = gs.expectations("-F/tmp/x=required ok-host=open a;b=open .x=open")
        assert hosts == [("ok-host", "open", "desktop-2")] and bad == [1, 3, 4]
        assert gs.read_host("-oProxyCommand=x") is None

    def test_dream_warns_when_the_unit_state_is_unknown_or_the_time_is_in_the_future(
        self,
    ):
        unknown = dream.assess([_night("2026-11-15", 5, GOOD)], {}, NOW)
        assert (
            unknown.startswith("CHECK palace-dream-night warn")
            and "unit state unreadable" in unknown
        )
        future = dream.assess([_night("2026-11-15", -3, GOOD)], RAN, NOW)
        assert future.startswith("CHECK palace-dream-night warn") and "future" in future
        naive = _night("2026-11-15", 5, GOOD)
        naive["generated_at"] = "2026-11-15T04:00:00"
        assert dream.assess([naive], RAN, NOW).startswith(
            "CHECK palace-dream-night fail"
        )

    def test_a_judge_error_shows_its_type_never_its_text(self):
        leak = {**GOOD, "error": "URLError: http://user:hunter2-secret@host/x"}
        out = dream.assess([_night("2026-11-15", 5, leak)], RAN, NOW)
        assert "URLError" in out and "hunter2" not in out

    def test_lines_are_one_line_bounded_and_labels_are_plain(self):
        text = checklib.line("x", "ok", "a\nb\r\nc " + "z" * 2000, "n\nx")
        assert "\n" not in text and "\r" not in text and len(text) < 1000
        assert checklib.label("wing\nname; rm -rf") == "wing?name??rm?-rf"
        out = secrets.assess([("jwt", ["evil\nwing"])], {}, {}, 0, 1, NOW)
        assert "\n" not in out and "evil?wing" in out

    def test_a_check_body_that_raises_becomes_a_fail_line(self):
        def boom():
            raise KeyError("x")

        with self.env(PALACE_CHECKS_DIR=str(self.tmp)):
            out = self.stdout(lambda: checklib.run("t-x", boom))
        assert out.startswith("CHECK t-x fail · the check itself raised KeyError")

    def test_an_unwritable_line_dir_is_reported_not_raised(self):
        blocker = self.tmp / "file"
        blocker.write_text("")
        with self.env(PALACE_CHECKS_DIR=str(blocker / "sub")):
            out = self.stdout(
                lambda: checklib.emit("t-y", checklib.line("t-y", "ok", "f"))
            )
        assert "line not saved" in out

    def test_read_var_reads_plain_and_export_forms_quietly(self):
        f = self.tmp / "env"
        f.write_text('# c\nexport A=x1\nB="y 2"\nC=\n')
        assert checklib.read_var(f, "A") == "x1" and checklib.read_var(f, "B") == "y 2"
        assert (
            checklib.read_var(f, "C") == ""
            and checklib.read_var(self.tmp / "none", "A") == ""
        )

    def test_a_hash_with_no_readable_stamp_is_not_called_new(self):
        since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
        rows = [
            ("aws", "cccc2222", "w1", "garbage"),
            ("aws", "dddd3333", "w1", "2026-11-14T08:59:00+00:00"),
            ("aws", "dddd3333", "w2", "2026-11-14T00:59:00"),
        ]  # naive local: earlier than 08:59Z
        assert secrets.new_secrets(rows, since) == []

    # --- review round 2

    def test_a_misspelt_host_state_fails_the_check_instead_of_dropping_the_host(self):
        def no_read(host):
            raise AssertionError("no host may be read")

        with (
            self.env(
                PALACE_CHECKS_DIR=str(self.tmp), GS_CHECK_HOSTS="a=required b=requird"
            ),
            mock.patch.object(gs, "read_host", no_read),
        ):
            out = self.stdout(gs.main)
        assert (
            out.startswith("CHECK palace-gs-auth fail")
            and "item(s) 2 not understood" in out
            and "requird" not in out
            and out.count("\n") == 1
        )

    def test_an_unsaved_line_is_still_one_line(self):
        blocker = self.tmp / "file"
        blocker.write_text("")
        with self.env(PALACE_CHECKS_DIR=str(blocker / "sub")):
            out = self.stdout(
                lambda: checklib.emit("t-z", checklib.line("t-z", "ok", "f"))
            )
        assert (
            out.count("\n") == 1
            and "line not saved" in out
            and out.startswith("CHECK t-z ok")
        )

    def test_a_unit_that_is_not_loaded_is_not_trusted(self):
        out = dream.assess(
            [_night("2026-11-15", 5, GOOD)], {**RAN, "LoadState": "not-found"}, NOW
        )
        assert out.startswith("CHECK palace-dream-night warn") and "not loaded" in out

    def test_a_judge_error_is_shown_as_a_type_or_a_fixed_marker(self):
        cases = [
            ("URLError: <urlopen error refused>", "URLError"),
            ("socket.timeout: timed out", "socket.timeout"),
            ("password=hunter2 in the prompt", "an error"),
            ("TimeoutError", "TimeoutError"),
            (
                "hunter2Error: failed",
                "an error",
            ),  # an Error-shaped secret is not a known type
            ("stopped: low memory", "stopped"),
        ]
        for err, shown in cases:
            with self.subTest(err=err):
                out = dream.assess(
                    [_night("2026-11-15", 5, {**GOOD, "error": err})], RAN, NOW
                )
                assert f"judge error ({shown})" in out and "hunter2" not in out

    def test_judge_counts_that_are_not_numbers_are_not_printed(self):
        con = {**OFF, "waiting": "60; rm -rf", "cached": -1}
        assert "off (? waiting, ? cached)" in dream.judge_state(con)

    def test_the_installer_creates_the_writable_dir_and_checks_the_configured_one(self):
        text = (CHECKS.parent / "systemd" / "install-checks.sh").read_text()
        assert 'mkdir -p "$lines"' in text and "PALACE_CHECKS_DIR must be" in text
        for unit in (CHECKS.parent / "systemd").glob("*.service"):
            body = unit.read_text()
            if "ReadWritePaths" in body:
                assert "ReadWritePaths=%h/.local/state/checks" in body

    def test_the_remote_takes_only_the_token_header_and_never_runs_the_host_curl_config(
        self,
    ):
        remote = gs.REMOTE
        assert '-K "$conf"' not in remote and "-K -" in remote and "curl -q" in remote

    def test_the_dream_check_asks_systemd_for_every_property_it_judges(self):
        (self.tmp / "2026-11-15.json").write_text(
            json.dumps(_night("2026-11-15", 1, GOOD))
        )
        seen = {}

        class Done:
            returncode = 0
            stdout = "LoadState=loaded\nResult=success\nExecMainStatus=0\nExecMainExitTimestamp=Sun x\n"

        def fake_run(argv, **kw):
            seen["argv"] = argv
            return Done()

        fixed = type("D", (dt.datetime,), {"now": staticmethod(lambda tz=None: NOW)})
        with (
            self.env(DREAM_OUT=str(self.tmp), PALACE_CHECKS_DIR=str(self.tmp / "c")),
            mock.patch.object(dream.subprocess, "run", fake_run),
            mock.patch.object(dream.dt, "datetime", fixed),
        ):
            out = self.stdout(dream.main)
        props = {seen["argv"][i + 1] for i, a in enumerate(seen["argv"]) if a == "-p"}
        assert {
            "LoadState",
            "Result",
            "ExecMainStatus",
            "ExecMainExitTimestamp",
            "ConditionTimestamp",
            "InactiveEnterTimestamp",
        } <= props
        assert "--timestamp=unix" in seen["argv"]
        assert out.startswith("CHECK palace-dream-night ok")

    def test_known_public_hashes_are_ignored_and_the_list_takes_only_kind_sha8(self):
        since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
        rows = [
            ("google", "34d4aaaa", "w1", "2026-11-14T10:00:00Z"),
            ("aws", "aaaa0000", "w1", "2026-11-14T10:00:00Z"),
        ]
        skip = secrets.ignored("google:34d4aaaa AIzaSyNOTAHASH aws:XYZ bad")
        assert skip == {("google", "34d4aaaa")}
        assert secrets.new_secrets(rows, since, skip) == [("aws", ["w1"])]

    def test_the_check_passes_its_ignore_list_to_the_judgement(self):
        # the body must hand SECRET_CHECK_IGNORE to new_secrets, not only parse it
        src = (CHECKS / "secret_drawers.py").read_text()
        body = src[src.index("def body()") :]
        assert (
            'ignored(os.environ.get("SECRET_CHECK_IGNORE"' in body
            and "new_secrets(" in body
        )

    # --- plan contract (FOR-AGENTS §24)

    def test_names_and_the_line_file_follow_the_plan_contract(self):
        assert (dream.NAME, secrets.NAME, gs.NAME) == (
            "palace-dream-night",
            "palace-secret-drawers",
            "palace-gs-auth",
        )
        with self.env(PALACE_CHECKS_DIR=str(self.tmp)):
            self.stdout(
                lambda: checklib.emit("palace-x", checklib.line("palace-x", "ok", "f"))
            )
        assert sorted(p.name for p in self.tmp.iterdir()) == [
            "palace-x.line"
        ]  # the .tmp was renamed
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PALACE_CHECKS_DIR", None)
            assert checklib.checks_dir() == Path.home() / ".local" / "state" / "checks"

    def test_the_gs_line_shows_labels_never_host_names(self):
        seen = []

        def fake_read(host):
            seen.append(host)
            return {
                "active": "active",
                "status": {"auth": {"required": True}},
                "events": [],
                "bwfix": 1,
                "tokenconf": True,
                "journal": True,
                "now": 1.0,
            }

        with (
            self.env(
                PALACE_CHECKS_DIR=str(self.tmp),
                GS_CHECK_HOSTS="realhostname1=required@desk-a",
            ),
            mock.patch.object(gs, "read_host", fake_read),
        ):
            out = self.stdout(gs.main)
        assert (
            seen == ["realhostname1"]
            and "desk-a required" in out
            and "realhostname1" not in out
        )

    def test_the_registry_has_the_plan_columns_and_parseable_cadences(self):
        text = (CHECKS.parent.parent / "docs" / "CHECKS.md").read_text()
        header = next(r for r in text.splitlines() if r.startswith("| name |"))
        cols = [c.strip() for c in header.strip("|").split("|")]
        for need in (
            "name",
            "host",
            "timer",
            "cadence",
            "measures",
            "owner",
            "the miss behind it",
        ):
            assert need in cols, need
        rows = [r for r in text.splitlines() if r.startswith("| palace-")]
        timers = sorted(
            p.name for p in (CHECKS.parent / "systemd").glob("palace-check-*.timer")
        )
        named = sorted(r.strip("|").split("|")[2].strip() for r in rows)
        assert named == timers, (
            named,
            timers,
        )  # one row per installed check timer, no more, no less
        cadence = re.compile(
            r"^(every \d+ min|every \d+ h|hourly|daily \d\d:\d\d|weekly (Mon|Tue|Wed|Thu|Fri|Sat|Sun) \d\d:\d\d)$"
        )
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            assert cadence.match(cells[cols.index("cadence")]), cells[0]
            assert re.fullmatch(r"[a-z0-9-]+", cells[0])

    def test_a_bad_item_naming_a_host_in_its_label_never_reaches_the_line(self):
        with self.env(
            PALACE_CHECKS_DIR=str(self.tmp),
            GS_CHECK_HOSTS="secrethost=requird@secrethost",
        ):
            out = self.stdout(gs.main)
        assert out.startswith("CHECK palace-gs-auth fail") and "secrethost" not in out

    def test_overlapping_writers_never_leave_a_partial_or_foreign_line(self):
        # Force the overlap: each rename waits, so without the lock a second writer truncates the
        # shared .tmp while the first is between write and rename, and one rename then fails.
        import threading
        import time

        real_replace = os.replace

        def slow_replace(src, dst):
            time.sleep(0.05)
            return real_replace(src, dst)

        lines = [
            checklib.line("palace-c", "ok", f"writer {i} " + "x" * 400)
            for i in range(6)
        ]
        # emit prints; capture once around the whole threaded run (redirect_stdout is global)
        buf = io.StringIO()
        with (
            self.env(PALACE_CHECKS_DIR=str(self.tmp)),
            mock.patch.object(checklib.os, "replace", slow_replace),
            contextlib.redirect_stdout(buf),
        ):
            threads = [
                threading.Thread(target=checklib.emit, args=("palace-c", t))
                for t in lines
            ]
            for th in threads:
                th.start()
            for th in threads:
                th.join()
        outs = buf.getvalue().splitlines()
        assert len(outs) == len(lines)
        assert not [o for o in outs if "not saved" in o], "a writer lost its rename"
        final = (self.tmp / "palace-c.line").read_text()
        assert final.rstrip("\n") in lines and final.count("\n") == 1
        assert not (self.tmp / "palace-c.line.tmp").exists()


if __name__ == "__main__":
    unittest.main()


class RedactNeedles(unittest.TestCase):
    """palace-redact-needles: mining stopped by a vault needle is a fail line naming the label only."""

    def test_probe_failure_is_fail_naming_the_label_only(self):
        state, fact = needles.assess(
            "vault item example-shared: vault answered rc 1, empty",
            ["example-shared"],
            [],
            1,
        )
        self.assertEqual(state, "fail")
        self.assertIn("mining stopped: vault item example-shared", fact)
        self.assertRegex(checklib.line(needles.NAME, state, fact), SHAPE)

    def test_refused_writes_in_the_window_fail_even_when_the_probe_now_loads(self):
        journal = [
            "ok line",
            "add failed: SecretPatternsUnavailable: vault item example-shared: vault answered rc 1, empty",
        ]
        state, fact = needles.assess(None, ["example-shared"], journal, 1)
        self.assertEqual(state, "fail")
        self.assertIn("1 palace write(s) refused", fact)
        self.assertIn("example-shared", fact)

    def test_quiet_window_with_needles_loaded_is_ok(self):
        state, fact = needles.assess(
            None, ["example-shared"], ["mine ok", "search ok"], 1
        )
        self.assertEqual(state, "ok")
        self.assertIn("1 needle(s) loaded (example-shared)", fact)

    def test_unreadable_journal_is_warn_and_a_missing_needle_is_warn(self):
        self.assertEqual(needles.assess(None, ["a"], None, 1)[0], "warn")
        self.assertEqual(needles.assess(None, [], ["x"], 1)[0], "warn")
        self.assertEqual(needles.assess(None, [], ["x"], 0)[0], "ok")
        self.assertEqual(
            needles.assess(None, ["a"], [], 1)[0], "warn"
        )  # empty window: blind

    def test_no_exception_text_or_unconfigured_label_is_ever_echoed(self):
        secret = "plainlettersecret"
        err = f"vault item example-shared: vault answered rc 1, {secret}"
        state, fact = needles.assess(err, ["example-shared"], ["x"], 1)
        self.assertEqual(state, "fail")
        self.assertNotIn(secret, fact)
        self.assertIn("vault-error", fact)
        journal = [
            f"SecretPatternsUnavailable: vault item {secret}: x",
            "SecretPatternsUnavailable: vault item example-shared: empty",
        ]
        state, fact = needles.assess(None, ["example-shared"], journal, 1)
        self.assertNotIn(secret, fact)
        self.assertIn("2 palace write(s) refused", fact)

    def test_journal_reads_the_system_unit(self):
        self.assertEqual(needles.JOURNAL_CMD[:3], ["journalctl", "-u", "palace-daemon"])
        self.assertNotIn("--user", needles.JOURNAL_CMD)


class PalaceSecrets(unittest.TestCase):
    """palace-secrets: the miner positive control, the strong-pass scan and the line. Fakes are built at run time."""

    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self._env = mock.patch.dict(os.environ, {"MEMPALACE_CONFIG_DIR": self.cfg})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        shutil.rmtree(self.cfg, ignore_errors=True)

    def _passes(self):
        try:
            import mempalace.redact  # noqa: F401
        except ImportError:
            self.skipTest("mempalace not importable")
        return psec.strong_passes()

    def test_every_fake_is_taken_by_the_miner_rules_under_its_own_kind(self):
        # the control proves nothing unless the miner's own rules take every fake, each as its kind
        _passes, ph = self._passes()
        from mempalace.redact import redact

        for _ in range(20):
            for kind, value in psec.make_fakes().items():
                out = redact(f"value {value} end")
                self.assertNotIn(value, out, kind)
                self.assertIn(ph(kind, value), out, kind)

    def test_the_probe_transcript_prints_every_fake(self):
        fakes = psec.make_fakes()
        text = psec.probe_transcript(fakes, "s1")
        rows = [json.loads(ln) for ln in text.splitlines()]
        self.assertEqual([r["type"] for r in rows], ["user", "assistant"])
        for value in fakes.values():
            self.assertIn(value, text)

    def test_the_probe_judge_needs_this_runs_own_placeholders(self):
        _passes, ph = self._passes()
        fakes = psec.make_fakes()
        own = " ".join(ph(k, v) for k, v in fakes.items())
        self.assertEqual(psec.judge_probe([], fakes, ph)[0], "not measured")
        self.assertEqual(
            psec.judge_probe([own], fakes, ph), ("ok", "1 probe drawer(s)")
        )
        state, detail = psec.judge_probe([own + " " + fakes["aws"]], fakes, ph)
        self.assertEqual((state, detail), ("FAIL", "raw aws"))
        self.assertNotIn(fakes["aws"], detail)
        # placeholders of the right kinds but for OTHER values (an old probe's) are not this run's: FAIL
        other = " ".join(ph(k, v) for k, v in psec.make_fakes().items())
        state, detail = psec.judge_probe([other], fakes, ph)
        self.assertEqual(state, "FAIL")
        self.assertIn("no placeholder for aws,github,litellm", detail)

    def test_strong_passes_take_providers_not_the_broad_shapes(self):
        passes, ph = self._passes()
        fakes = psec.make_fakes()
        broad = (
            "Kx" + "9q" * 6 + "Zr4tLm"
        )  # a mixed-case token with no provider prefix: a broad shape
        text = f"aws {fakes['aws']} and an identifier {broad}"
        out, kinds = psec.strong_redact(text, passes, ph)
        self.assertNotIn(fakes["aws"], out)
        self.assertIn(broad, out)
        self.assertEqual(dict(kinds), {"aws": 1})

    def test_metadata_values_and_keys_are_redacted_at_any_depth(self):
        passes, ph = self._passes()
        fakes = psec.make_fakes()
        meta = {
            "a": [{"b": f"tok {fakes['github']}"}],
            "n": 3,
            fakes["aws"]: "ordinary text",
        }
        out, kinds = psec.strong_redact_value(meta, passes, ph)
        self.assertNotIn(fakes["github"], json.dumps(out))
        self.assertNotIn(fakes["aws"], json.dumps(out))
        self.assertEqual(out["n"], 3)
        self.assertEqual(dict(kinds), {"github": 1, "aws": 1})

    def test_a_redacted_key_never_overwrites_another(self):
        passes, ph = self._passes()
        aws = psec.make_fakes()["aws"]
        placeholder_key = ph("aws", aws)
        out, _k = psec.strong_redact_value(
            {placeholder_key: "kept", aws: "also kept"}, passes, ph
        )
        self.assertEqual(sorted(out.values()), ["also kept", "kept"])

    def test_a_wing_shaped_like_a_secret_never_reaches_the_line(self):
        passes, ph = self._passes()
        aws = psec.make_fakes()["aws"]
        text = psec.assess(
            {(aws, "aws"): 1}, 0, 0, 10, 5, 0, ("ok", "x"), False, None, 0, passes, ph
        )
        self.assertNotIn(aws, text)
        self.assertIn("<wing name holds a secret shape>", text)

    def test_the_line(self):
        ok = ("ok", "1 probe drawer(s)")
        cases = {
            "ok": psec.assess({}, 0, 0, 100, 50, 0, ok, False),
            "warn_hits": psec.assess(
                {("money", "aws"): 2}, 0, 0, 100, 50, 0, ok, False
            ),
            "warn_probe": psec.assess(
                {}, 0, 0, 100, 50, 0, ("not measured", "mining paused"), False
            ),
            "warn_left": psec.assess({}, 0, 0, 100, 50, 0, ok, False, None, 2),
            "fail_miner": psec.assess({}, 0, 0, 100, 50, 0, ("FAIL", "raw aws"), False),
            "fail_left": psec.assess({("jp", "pem"): 1}, 0, 1, 100, 50, 0, ok, True),
            "fail_error": psec.assess(
                {}, 0, 0, 0, 0, 0, ("not measured", "x"), False, "OperationalError"
            ),
        }
        for name, text in cases.items():
            self.assertRegex(text, SHAPE, name)
        self.assertIn(" ok · ", cases["ok"])
        self.assertIn("miner redaction: ok", cases["ok"])
        self.assertIn(" warn · ", cases["warn_hits"])
        self.assertIn("money (aws×2)", cases["warn_hits"])
        self.assertIn("PALACE_SECRETS_REDACT=1", cases["warn_hits"])
        self.assertIn(
            "miner redaction: not measured (mining paused)", cases["warn_probe"]
        )
        self.assertIn(" warn · ", cases["warn_left"])
        self.assertIn("2 probe leftover(s)", cases["warn_left"])
        self.assertIn(" fail · ", cases["fail_miner"])
        self.assertIn(" fail · ", cases["fail_left"])
        self.assertIn("1 NOT redacted", cases["fail_left"])
        self.assertIn(" fail · ", cases["fail_error"])

    def test_the_skip_reason_is_never_echoed(self):
        with (
            tempfile.TemporaryDirectory() as d,
            mock.patch.object(
                psec,
                "_daemon",
                return_value=(200, {"skipped": True, "reason": "anything at all"}),
            ),
        ):
            _f, path, early = psec.probe_start("k", Path(d))
            self.assertEqual(early, ("not measured", "mining paused"))
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")

    def test_no_credential_shaped_literal_in_the_check(self):
        # the strong passes (provider shapes, keys) find nothing in the source: every fake is assembled at run time
        passes, ph = self._passes()
        src = (CHECKS / "palace_secrets.py").read_text(encoding="utf-8")
        self.assertEqual(dict(psec.strong_redact(src, passes, ph)[1]), {})

    def test_probe_rows_are_judged_never_redacted(self):
        pdir = Path("/x/palace_probe/probe")
        meta = {"source_file": "/x/palace_probe/probe/s.jsonl"}
        far = {"source_file": "/x/palaceXprobe/probe/s.jsonl"}
        own = ("mempalace_drawers", "d1", "palace_probe", "", meta, {}, ())
        other = ("mempalace_drawers", "d2", "jp", "", far, {}, ())
        private = ("mempalace_private_drawers", "d3", "palace_probe", "", meta, {}, ())
        self.assertTrue(psec._is_probe_row(own, pdir))
        self.assertFalse(psec._is_probe_row(other, pdir))
        # a private row is never exempt: the probe cleanup does not reach that table
        self.assertFalse(psec._is_probe_row(private, pdir))

    def test_drawer_cleanup_matches_the_probe_dir_literally(self):
        # "_" and "%" in the path must not act as wildcards (LIKE would delete unrelated drawers)
        seen = {}

        class Conn:
            def execute(self, sql, params):
                seen["sql"], seen["params"] = sql, params
                return mock.Mock(fetchall=list)

        self.assertEqual(
            psec.probe_cleanup(Conn(), "k", Path("/tmp/palace_probe/probe")), 0
        )
        self.assertIn("starts_with(", seen["sql"])
        self.assertNotIn("LIKE", seen["sql"])
        self.assertEqual(seen["params"], ("/tmp/palace_probe/probe/",))

    def test_cleanup_that_cannot_look_counts_as_left(self):
        class Broken:
            def execute(self, *_a):
                raise RuntimeError("connection is closed")

        self.assertEqual(psec.probe_cleanup(Broken(), "k", Path("/p")), 1)

    def test_probe_files_go_without_a_database(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.jsonl").write_text("x")
            (Path(d) / "b.jsonl").write_text("x")
            self.assertEqual(psec.probe_files_cleanup(Path(d)), 0)
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_old_backups_of_both_formats_are_pruned(self):
        with tempfile.TemporaryDirectory() as d:
            old = time.time() - (psec.BACKUP_DAYS + 1) * 86400
            for name in ("old.jsonl", "old.copy", "new.jsonl"):
                f = Path(d) / name
                f.write_text("x")
                if name.startswith("old"):
                    os.utime(f, (old, old))
            psec.prune_backups(Path(d))
            self.assertEqual(sorted(x.name for x in Path(d).iterdir()), ["new.jsonl"])


class PalaceSecretsClassify(unittest.TestCase):
    """palace-secrets classify: an encoding observation per hit, never a value (Sen 2026-11-17; xreview r1)."""

    @staticmethod
    def _rand(alphabet, n, seed):
        import random

        rnd = random.Random(seed)
        return "".join(rnd.choice(alphabet) for _ in range(n))

    UP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    PW = "abcdefghijkmnopqrstuvwxyz23456789"

    @staticmethod
    def _b64(obj):
        import base64 as b64

        return b64.b64encode(json.dumps(obj).encode()).decode()

    def _assert_free(self, out, *secrets):
        for sec in secrets:
            self.assertNotIn(sec, out)
            for i in range(0, max(len(sec) - 6, 1), 3):
                self.assertNotIn(sec[i : i + 6], out, "a 6-character fragment leaked")

    def test_prefixes_and_hex_are_observations_never_verdicts(self):
        for value, want in {
            "AKIA" + self._rand(self.UP, 16, 1): "AWS long-term",
            "ASIA" + self._rand(self.UP, 16, 2): "AWS temporary",
            "ghs_" + self._rand(self.UP + self.PW, 36, 3): "installation token (typically 1 h)",
        }.items():
            got = pclass.shape(value)
            self.assertIn(want, got)
            self.assertIn("validity unknown", got)
            self._assert_free(got, value[4:])
        for n in (32, 40, 64):
            h = self._rand("0123456789abcdef", n, n)
            got = pclass.shape(h)
            self.assertEqual(got, f"hex-{n} (hash-like format; validity unknown)")

    def test_only_an_exact_published_example_is_named(self):
        self.assertIn("exact match", pclass.shape("AKIA" + "IOSFODNN7" + "EXAMPLE"))
        live = "AKIA" + self._rand(self.UP, 9, 4) + "EXAMPLE"
        self.assertNotIn("example", pclass.shape(live).lower())

    def test_unknown_json_key_names_are_never_shown(self):
        pw = self._rand(self.PW, 14, 5)
        got = pclass.shape(self._b64({"type": "service_account", "private_key_id": "x", pw: "y"}))
        self.assertIn("keys [type,private_key_id,<other>] type=service_account", got)
        self._assert_free(got, pw)
        cut = self._b64({"kind": "tiles", pw: 1, "more": [1] * 40})
        cut = cut[: len(cut) - len(cut) % 4 - 7]
        got = pclass.shape(cut)
        self.assertIn("keys seen [kind,<other>", got)
        self._assert_free(got, pw)

    def test_a_jwt_shows_only_a_listed_alg_and_an_unverified_exp(self):
        import base64 as b64

        def seg(o):
            return b64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")

        pw = self._rand(self.PW, 12, 6)
        sig = self._rand(self.PW, 43, 7)
        got = pclass.shape(".".join((seg({"alg": "HS256"}), seg({"sub": pw, "exp": 1}), sig)))
        self.assertEqual(got, "jwt alg=HS256, unverified exp claim 1970-01-01 (past)")
        got = pclass.shape(".".join((seg({"alg": pw}), seg({"exp": 10**30}), sig)))
        self.assertEqual(got, "jwt alg=<other>, invalid exp claim")
        got = pclass.shape(".".join((seg({"alg": "none"}), "eyJleHAiOiBOYU59", sig)))  # {"exp": NaN}
        self.assertIn("invalid exp claim", got)
        # xreview r2: a huge integer exp and a non-string alg never raise
        big = b64.urlsafe_b64encode(b'{"exp": 1' + b"0" * 400 + b"}").decode().rstrip("=")
        self.assertEqual(pclass.shape(".".join((seg({"alg": ["HS256"]}), big, sig))), "jwt alg=<other>, invalid exp claim")
        self.assertEqual(pclass.shape(".".join((seg({"alg": {}}), seg({}), sig))), "jwt alg=<other>")
        huge = b64.b64encode(b'{"type": "service_account", "exp": 1' + b"0" * 400 + b"}").decode()
        self.assertIn("invalid exp claim", pclass.shape(huge))

    def test_deeply_nested_json_never_raises(self):
        # xreview r3: json.loads raises RecursionError on deep nesting
        import base64 as b64

        deep = b"[" * 60000 + b"0" + b"]" * 60000
        self.assertIn("base64 of", pclass.shape(b64.b64encode(deep).decode()))
        nested = b'{"a":' * 60000 + b"0" + b"}" * 60000  # starts with eyJ once encoded
        seg = b64.urlsafe_b64encode(nested).decode().rstrip("=")
        self.assertEqual(pclass.shape(".".join((seg, seg, "abc"))), "jwt alg=<other>")

    def test_binary_that_starts_with_a_brace_is_called_binary(self):
        import base64 as b64
        import random

        rnd = random.Random(11)
        raw = b"{" + bytes(rnd.randrange(128, 256) for _ in range(300))
        got = pclass.shape(b64.b64encode(raw).decode())
        self.assertIn("binary", got)
        self.assertNotIn("JSON", got)

    def test_classify_row_end_to_end_with_adversarial_text(self):
        import re as _re

        secret = "AKIA" + self._rand("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567", 16, 9)
        pw = self._rand(self.PW, 14, 10)
        pat = _re.compile(r"(?P<_mprkeep>(?!))|(?P<_mpr0>AKIA[A-Z2-7]{16})")
        passes = [(pat, ["aws"])]

        def safe_fn(text):
            return pat.sub("[redacted aws]", text)

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR_TESTS")) as d:
            proj = Path(d)
            (proj / pw).mkdir()  # xreview r2: a directory that exists is not a safe label
            uuid = "0a1b2c3d-1111-2222-3333-444455556666"
            frag = secret[6:14]
            cases = [
                (f"# deploy notes\nthe key {secret} was printed", {"source_file": f"{d}/{pw}/{uuid}.jsonl"},
                 "transcript session 0a1b2c3d", "# deploy notes"),
                (f"# password: {pw}\n{secret}", {"source_file": f"{d}/{pw}/x.jsonl"}, "other file", "<withheld>"),
                (f"# tab\there {pw}{pw}\n{secret}", {"source_file": f"/elsewhere/{pw}.jsonl"}, "other file", "<withheld>"),
                (f"# {frag}\n{secret}", {}, "none", "<withheld>"),  # a short fragment of the credential
                (f"# notes {pw[:6]}\n{secret}", {}, "none", "<withheld>"),  # digits: not plain words
            ]
            for doc, meta, want_src, want_head in cases:
                lines = pclass.classify_row("mempalace_drawers", "d1", doc, meta, passes, safe_fn, proj)
                self.assertEqual(len(lines), 1)
                cols = lines[0].split("\t")
                self.assertEqual(len(cols), 8)
                self.assertEqual(cols[2], want_src)
                self.assertEqual(cols[3], "document")
                self.assertEqual(cols[7], want_head)
                self._assert_free(lines[0], secret[4:], pw)
            lines = pclass.classify_row(
                "mempalace_drawers", "d2", "", {pw: secret, "topic": secret}, passes, safe_fn, proj
            )
            self.assertEqual([ln.split("\t")[3] for ln in lines], ["metadata.<other>", "metadata.topic"])
            for ln in lines:
                self._assert_free(ln, secret[4:], pw)

    def test_usage_without_a_wing(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(pclass.main([]), 22)


def _pg_log(*statements, pid=77, t="22:46:40.123"):
    """A fictional postgres log: a checkpoint record, then per statement an ERROR (cancellation) record and its
    STATEMENT record; a statement given as a list spans continuation lines."""
    head = f"2026-11-18 {t} UTC [{pid}] "
    out = ["2026-11-18 22:40:00.000 UTC [33] LOG:  checkpoint starting: time"]
    for st in statements:
        lines = st if isinstance(st, list) else [st]
        out.append(head + "ERROR:  canceling statement due to statement timeout")
        out.append(head + "STATEMENT:  " + lines[0])
        out.extend("\t" + x for x in lines[1:])
    return out


class PgTimeouts(unittest.TestCase):
    """palace-pg-timeouts: cancellation RECORDS by fixed kind code, paired by backend pid; never statement text."""

    WING = "SELECT wing, count(*) FROM mempalace_drawers GROUP BY wing ORDER BY count(*) DESC"
    GRAPH = ["", "SELECT", "room,", "array_agg(DISTINCT wing ORDER BY wing) AS wings,", "FROM g GROUP BY room"]
    META = "SELECT id, NULL::text, wing, room, metadata FROM \"mempalace_drawers\" WHERE (metadata->>$1 = $2) ORDER BY id LIMIT $3"
    PAGE = "SELECT id, document, wing, room, metadata FROM \"mempalace_drawers\" WHERE ((\"wing\" = $1) AND (\"room\" = $2)) ORDER BY id LIMIT $3"

    def test_kinds_are_counted_including_a_multi_line_statement(self):
        c = pgt.cancellations(_pg_log(self.WING, self.GRAPH, self.META, self.PAGE, self.PAGE))
        self.assertEqual(c["status-wing-count"], 1)
        self.assertEqual(c["graph-room-aggregate"], 1)
        self.assertEqual(c["metadata-lookup"], 1)
        self.assertEqual(c["wing-page"], 2)
        self.assertEqual(sum(c.values()), 5)

    def test_the_phrase_inside_a_statement_or_detail_is_not_a_cancellation(self):
        log = _pg_log("SELECT 'canceling statement due to statement timeout'")
        log.append("2026-11-18 22:46:41.000 UTC [77] DETAIL:  parameters: $1 = 'canceling statement due to statement timeout'")
        log.append("\tcanceling statement due to statement timeout")  # a continuation line of drawer text
        self.assertEqual(sum(pgt.cancellations(log).values()), 1)

    def test_statements_pair_with_their_own_backend(self):
        a = "2026-11-18 22:46:40.100 UTC [101] "
        b = "2026-11-18 22:46:40.100 UTC [202] "
        log = [
            a + "ERROR:  canceling statement due to statement timeout",
            b + "ERROR:  canceling statement due to statement timeout",
            b + "STATEMENT:  " + self.WING,
            a + "STATEMENT:  " + self.META,
        ]
        c = pgt.cancellations(log)
        self.assertEqual(c["status-wing-count"], 1)
        self.assertEqual(c["metadata-lookup"], 1)

    def test_a_cancellation_without_a_statement_is_unknown(self):
        log = ["2026-11-18 22:46:40.100 UTC [5] ERROR:  canceling statement due to statement timeout",
               "2026-11-18 22:46:41.100 UTC [5] LOG:  disconnection: session time"]
        self.assertEqual(pgt.cancellations(log)["unknown"], 1)

    def test_wrapped_sql_and_misleading_literals(self):
        wrapped = ["SELECT wing,", "   count(*)   FROM   mempalace_drawers", "GROUP   BY wing"]
        insert = "INSERT INTO \"mempalace_drawers\" (id, document) VALUES ($1, 'array_agg(DISTINCT wing text')"
        literal_only = "SELECT 1 WHERE 'x' = 'metadata->> cypher( <=>'"
        c = pgt.cancellations(_pg_log(wrapped, insert, literal_only))
        self.assertEqual(c["status-wing-count"], 1)
        self.assertEqual(c["drawer-insert"], 1)
        self.assertEqual(c["other"], 1)

    def test_a_long_statement_is_not_truncated(self):
        long = ["SELECT"] + [f"  col{i}," for i in range(80)] + ["  array_agg(DISTINCT wing ORDER BY wing)", "FROM g"]
        self.assertEqual(pgt.cancellations(_pg_log(long))["graph-room-aggregate"], 1)

    def test_pairing_has_no_window(self):
        a = "2026-11-18 22:46:40.100 UTC [101] "
        log = [a + "ERROR:  canceling statement due to statement timeout"]
        log += [f"2026-11-18 22:46:40.200 UTC [{300 + i}] LOG:  connection received" for i in range(500)]
        log += [a + "DETAIL:  while scanning", a + "STATEMENT:  " + self.META]
        self.assertEqual(dict(pgt.cancellations(log)), {"metadata-lookup": 1})

    def test_a_backend_that_moves_on_without_a_statement_is_unknown_once(self):
        a = "2026-11-18 22:46:40.100 UTC [101] "
        log = [a + "ERROR:  canceling statement due to statement timeout", a + "LOG:  duration: 1 ms",
               a + "STATEMENT:  " + self.META]
        self.assertEqual(dict(pgt.cancellations(log)), {"unknown": 1})

    def test_escape_and_dollar_quoted_literals_are_masked(self):
        esc = "SELECT E'can\\'t metadata->>' AS x"
        dollar = "SELECT $body$ metadata->> <=> cypher( $body$ AS y"
        cyph = "SELECT * FROM cypher('mempalace_kg', $$ MATCH (n) WHERE n.x = 'metadata->>' RETURN n $$) AS (n agtype)"
        c = pgt.cancellations(_pg_log(esc, dollar, cyph, self.PAGE))
        self.assertEqual(c["other"], 2)
        self.assertEqual(c["kg-cypher"], 1)
        self.assertEqual(c["wing-page"], 1)  # $1 / $2 parameters are not dollar quotes

    def test_thresholds(self):
        self.assertEqual(pgt.assess(_pg_log())[0], "ok")
        self.assertEqual(pgt.assess(_pg_log(*[self.WING] * 10))[0], "ok")
        self.assertEqual(pgt.assess(_pg_log(*[self.WING] * 11))[0], "warn")
        state, fact = pgt.assess(_pg_log(*[self.WING] * 60))
        self.assertEqual(state, "fail")
        self.assertIn("60 statement timeout(s)", fact)
        self.assertRegex(checklib.line(pgt.NAME, state, fact), SHAPE)
        self.assertEqual(pgt.assess(_pg_log(self.WING, self.WING), ok_per_h=0, fail_per_h=2)[0], "fail")

    def test_invalid_thresholds_are_a_fail_line_not_a_crash(self):
        for ok, fail in (("x", "60"), ("-1", "60"), ("10", "10"), ("20", "5")):
            with mock.patch.dict(os.environ, {"PG_TIMEOUTS_OK_PER_H": ok, "PG_TIMEOUTS_FAIL_PER_H": fail}):
                with self.assertRaises(ValueError):
                    pgt.thresholds()
                with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PALACE_CHECKS_DIR": d}), \
                        contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(pgt.run(pgt.NAME, pgt.body), 0)
                self.assertIn("fail · invalid thresholds", out.getvalue())

    def test_unreadable_is_fail_and_an_empty_window_is_blind_warn(self):
        self.assertEqual(pgt.assess(None)[0], "fail")
        self.assertEqual(pgt.assess([])[0], "warn")
        self.assertEqual(pgt.assess(["\tjust a continuation"])[0], "warn")

    def test_no_statement_text_or_parameter_reaches_the_line(self):
        secret = "plainlettersecretvalue"
        st = f"SELECT count(*) FROM mempalace_drawers WHERE wing=$1 AND document ~ '{secret}'"
        log = _pg_log(st, f"UPDATE x SET y = '{secret}'")
        log.append("2026-11-18 22:46:40.123 UTC [77] DETAIL:  parameters: $1 = '" + secret + "'")
        state, fact = pgt.assess(log)
        self.assertNotIn(secret, fact)
        self.assertNotIn("SELECT", fact)
        self.assertIn("2 statement timeout(s)", fact)

    def test_reads_the_container_log_for_the_window(self):
        self.assertEqual(pgt.LOG_CMD[:2], ["docker", "logs"])
        self.assertIn(f"{pgt.WINDOW_MIN}m", pgt.LOG_CMD)
