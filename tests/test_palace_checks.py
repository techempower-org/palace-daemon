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
pbk = _load("palace_backup")
pvac = _load("palace_vacuum")

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


class PalaceSecretsProbeWait(unittest.TestCase):
    """A not-measured miner control says what the wait saw at its deadline (counts only)."""

    def _wait(self, rows):
        conn = mock.MagicMock()
        with mock.patch.object(psec, "_rows_for", return_value=rows), mock.patch.object(psec.time, "sleep"), \
                mock.patch.object(psec.time, "time", side_effect=[0, 0, 10**9]):
            return psec.probe_wait(conn, Path("/p/x.jsonl"), {}, None, deadline=1)

    def test_not_mined(self):
        state, why = self._wait([])
        self.assertEqual(state, "not measured")
        self.assertIn("not mined: 0 drawers", why)

    def test_fewer_chunks_than_chunk_total(self):
        state, why = self._wait([("d1", "doc", "{}", 2)])
        self.assertEqual(state, "not measured")
        self.assertIn("1 of 2 chunk(s) filed", why)

    def test_drawers_without_chunk_total(self):
        self.assertIn("1 drawer(s) without chunk_total", self._wait([("d1", "doc", "{}", None)])[1])

    def test_all_filed_but_not_stable_names_the_unmet_condition(self):
        # review #344 r1 (2): the deadline expired on stability, not on the count
        rows = [("d1", "doc", "{}", 2), ("d2", "doc", "{}", 2)]
        with mock.patch.object(psec, "_rows_for", return_value=rows), mock.patch.object(psec.time, "sleep"), \
                mock.patch.object(psec.time, "time", side_effect=[0, 10**9, 10**9]):   # one read, then the deadline
            state, why = psec.probe_wait(mock.MagicMock(), Path("/p/x.jsonl"), {}, None, deadline=1)
        self.assertEqual(state, "not measured")
        self.assertIn("2 of 2 filed, count not yet stable", why)

    def test_an_ok_probe_names_its_lane_the_backlog_and_the_request_path_instrument(self):
        # review #344 r1 (1)+(3): priority 1 hides the p2 backlog unless the line names it; the request path is
        # the daemon watchdog's job, not this probe's
        txt = psec.lane_text({"queued_by_priority": {"2": 14, "1": 0}, "oldest_waiting_s": {"2": 1800},
                              "queue_read_errors": 0})
        self.assertEqual(txt, "probe lane p1 · queue depth 14 · oldest p2 item waiting 1800s · request path: daemon watchdog")
        self.assertIn("no p2 item waiting", psec.lane_text({"queued_by_priority": {}, "oldest_waiting_s": {}, "queue_read_errors": 0}))
        self.assertIn("queue: unavailable (incomplete status)", psec.lane_text(None))
        out = psec.assess({}, 0, 0, 10, 0, 0, ("ok", txt), True)
        self.assertIn("miner redaction: ok (probe lane p1 · queue depth 14", out)

    def test_an_unavailable_queue_snapshot_is_warn_never_depth_0(self):
        # review #344 r2: a 503 (or timeout, or a body without the fields) must not read as an empty queue
        import urllib.error
        err = urllib.error.HTTPError("http://x/mine/status", 503, "unavailable", {}, None)
        with mock.patch.object(psec, "_daemon", side_effect=err):
            body, why = psec.queue_status("k")
        self.assertIsNone(body)
        self.assertEqual(why, "HTTP 503")
        with mock.patch.object(psec, "_daemon", side_effect=TimeoutError()):
            self.assertEqual(psec.queue_status("k")[1], "TimeoutError")
        with mock.patch.object(psec, "_daemon", return_value=(200, {})):
            self.assertEqual(psec.queue_status("k")[1], "incomplete status")
        txt = psec.lane_text(None, "HTTP 503")
        self.assertNotIn("depth 0", txt)
        self.assertIn("queue: unavailable (HTTP 503)", txt)
        out = psec.assess({}, 0, 0, 10, 0, 0, ("ok", txt), True)
        self.assertTrue(out.startswith("CHECK palace-secrets warn"), out)
        ok = psec.lane_text({"queued_by_priority": {"2": 0}, "oldest_waiting_s": {}, "queue_read_errors": 0})
        self.assertTrue(psec.assess({}, 0, 0, 10, 0, 0, ("ok", ok), True).startswith("CHECK palace-secrets ok"))

    def test_an_incomplete_queue_status_is_unavailable_and_warn(self):
        # review #344 r3: oldest_waiting_s missing or non-numeric must not read ok
        for body in ({"queued_by_priority": {"2": 3}},
                     {"queued_by_priority": {"2": 3}, "oldest_waiting_s": {"2": "soon"}},
                     {"queued_by_priority": {"2": "3"}, "oldest_waiting_s": {"2": 10}},
                     {"queued_by_priority": {"2": True}, "oldest_waiting_s": {}}):
            with mock.patch.object(psec, "_daemon", return_value=(200, body)):
                got, why = psec.queue_status("k")
            self.assertIsNone(got, body)
            self.assertEqual(why, "incomplete status")
            txt = psec.lane_text(body)
            self.assertIn("queue: unavailable (incomplete status)", txt)
            self.assertTrue(psec.assess({}, 0, 0, 10, 0, 0, ("ok", txt), True).startswith("CHECK palace-secrets warn"))

    def test_a_priority_with_entries_needs_an_oldest_wait(self):
        # review #344 r4: p2 has entries but no oldest wait: incomplete, never ok
        for body in ({"queued_by_priority": {"2": 3}, "oldest_waiting_s": {}, "queue_read_errors": 0},
                     {"queued_by_priority": {"1": 1, "2": 3}, "oldest_waiting_s": {"1": 5}, "queue_read_errors": 0},
                     {"queued_by_priority": {"2": 3}, "oldest_waiting_s": {"2": 9}}):
            with mock.patch.object(psec, "_daemon", return_value=(200, body)):
                self.assertEqual(psec.queue_status("k"), (None, "incomplete status"), body)
            txt = psec.lane_text(body)
            self.assertIn("queue: unavailable (incomplete status)", txt)
            self.assertTrue(psec.assess({}, 0, 0, 10, 0, 0, ("ok", txt), True).startswith("CHECK palace-secrets warn"))
        whole = {"queued_by_priority": {"1": 0, "2": 3}, "oldest_waiting_s": {"2": 9}, "queue_read_errors": 0}
        self.assertIsNone(psec.queue_gap(whole))

    def test_queue_read_errors_are_unavailable_and_warn(self):
        # review #344 r4: an unreadable queue file must not read as an empty queue
        body = {"queued_by_priority": {}, "oldest_waiting_s": {}, "queue_read_errors": 1}
        with mock.patch.object(psec, "_daemon", return_value=(200, body)):
            self.assertEqual(psec.queue_status("k"), (None, "1 read errors"))
        txt = psec.lane_text(body)
        self.assertIn("queue: unavailable (1 read errors)", txt)
        self.assertNotIn("depth 0", txt)
        self.assertTrue(psec.assess({}, 0, 0, 10, 0, 0, ("ok", txt), True).startswith("CHECK palace-secrets warn"))

    def test_no_drawer_text_in_the_reason(self):
        why = self._wait([("d1", "plainlettersecretvalue", "{}", 3)])[1]
        self.assertNotIn("plainlettersecretvalue", why)


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
        # bare $$…$$ directly, with nothing misleading in ordinary quotes (xreview r3)
        self.assertEqual(pgt.kind("SELECT $$ metadata->> $$"), "other")
        self.assertEqual(pgt.kind("SELECT * FROM cypher('g', $$ array_agg(DISTINCT wing) $$)"), "kg-cypher")
        self.assertEqual(pgt.kind("SELECT $q$ <=> $q$, $$ ts_rank_cd $$"), "other")
        self.assertEqual(pgt.kind('SELECT id FROM "mempalace_drawers" WHERE ("wing" = $1) AND id > $2'), "wing-page")

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


class PalaceBackup(unittest.TestCase):
    """palace-backup: the schedule, the newest dump, its change, the unit's result, integrity bound to its dump."""

    NOW = 1_800_000_000.0
    TRIG = NOW - 8 * 3600                       # last night's trigger, 8 h ago

    def unit(self, **kw):
        u = {"Result": "success", "ExecMainStatus": "0", "ActiveState": "inactive",
             "LastTriggerUSec": f"@{int(self.TRIG)}", "ExecMainExitTimestamp": f"@{int(self.TRIG + 600)}",
             "TimerNext": f"@{int(self.TRIG + 86400)}", "TimerActive": "active", "TimerEnabled": "enabled"}
        u.update(kw)
        return u

    def _dumps(self, sizes, newest_mtime=None):
        newest = self.TRIG + 600 if newest_mtime is None else newest_mtime
        n = len(sizes)
        return [(f"mempalace-202611{10 + i:02d}.sql.gz", s, newest - 86400 * (n - 1 - i)) for i, s in enumerate(sizes)]

    def good(self, found):
        """A completed run's fields (wrapped as last_completed by ``wrap``)."""
        name, size, mtime = found[-1]
        return {"result": "ok", "dump": name, "size": size, "mtime": mtime,
                "started": self.NOW - 2 * 86400, "at": self.NOW - 2 * 86400}

    @staticmethod
    def wrap(rec, running_since=None):
        return None if rec is None else {"last_completed": rec, "running_since": running_since}

    def _assess(self, sizes, unit=None, integrity="good", current="same", newest_mtime=None, timer="active"):
        found = self._dumps(sizes, newest_mtime)
        rec = self.wrap(self.good(found)) if integrity == "good" else integrity
        rec = self.wrap(rec) if rec and "result" in rec else rec
        cur = (found[-1][1], found[-1][2]) if current == "same" else current
        return pbk.assess(found, self.NOW, unit or self.unit(), timer, rec, cur)

    def test_a_fresh_growing_dump_set_is_ok(self):
        state, fact = self._assess([4_000_000_000, 4_100_000_000, 4_200_000_000])
        self.assertEqual(state, "ok", fact)
        self.assertIn("4.20 GB", fact)
        self.assertIn("3 kept", fact)

    def test_no_dump_fails(self):
        self.assertEqual(pbk.assess([], self.NOW, self.unit(), "active", None)[0], "fail")

    def test_a_missed_night_fails_although_yesterdays_dump_is_young(self):
        # review r1 (1): the timer fired 8 h ago, nothing ran; yesterday's dump is 20 h old (< 26 h)
        unit = self.unit(ExecMainExitTimestamp=f"@{int(self.TRIG - 86400 + 600)}")
        state, fact = self._assess([4e9, 4e9, 4e9], unit=unit, newest_mtime=self.TRIG - 12 * 3600)
        self.assertEqual(state, "fail", fact)
        self.assertIn("no dump run finished after the scheduled", fact)
        self.assertIn("wrote no dump", fact)

    def test_a_dump_still_running_soon_after_its_trigger_warns_never_ok(self):
        # review r2 (1): a hung backup inside the grace window must not read ok on yesterday's dump
        trig = self.NOW - 1800
        unit = self.unit(ActiveState="active", LastTriggerUSec=f"@{int(trig)}", TimerNext=f"@{int(trig + 86400)}",
                         ExecMainExitTimestamp=f"@{int(trig - 86400 + 600)}")
        state, fact = self._assess([4e9, 4e9, 4e9], unit=unit, newest_mtime=trig - 86400 + 600)
        self.assertEqual(state, "warn", fact)
        self.assertIn("backup still running since", fact)

    def test_a_timer_that_never_fired_last_night_fails(self):
        # review r4: last trigger two days old, last night's scheduled time passed with no run: never ok
        two_days = self.TRIG - 2 * 86400
        unit = self.unit(LastTriggerUSec=f"@{int(two_days)}", ExecMainExitTimestamp=f"@{int(two_days + 600)}")
        state, fact = self._assess([4e9, 4e9, 4e9], unit=unit, newest_mtime=two_days + 600)
        self.assertEqual(state, "fail", fact)
        self.assertIn("no dump run finished after the scheduled", fact)
        self.assertIn("wrote no dump", fact)

    def test_the_scheduled_run_not_yet_finished_inside_its_grace_warns(self):
        sched = self.NOW - 1800
        unit = self.unit(LastTriggerUSec=f"@{int(sched - 86400)}", TimerNext=f"@{int(sched + 86400)}",
                         ExecMainExitTimestamp=f"@{int(sched - 86400 + 600)}")
        state, fact = self._assess([4e9] * 3, unit=unit, newest_mtime=sched - 86400 + 600)
        self.assertEqual(state, "warn", fact)
        self.assertIn("has not finished yet", fact)

    def test_an_inactive_or_disabled_timer_fails_on_its_own(self):
        self.assertEqual(self._assess([4e9] * 3, unit=self.unit(TimerActive="inactive"))[0], "fail")
        self.assertEqual(self._assess([4e9] * 3, unit=self.unit(TimerEnabled="disabled"))[0], "fail")
        self.assertEqual(self._assess([4e9] * 3, unit=self.unit(TimerNext=""))[0], "fail")

    def test_the_integrity_mode_judges_the_state_after_its_run(self):
        # review r2 (2): the run takes up to an hour; the line must use a fresh inventory and unit state
        before = [("mempalace-20261119.sql.gz", 4e9, self.NOW - 30 * 3600)]
        after = before + [("mempalace-20261120.sql.gz", 4.1e9, self.NOW - 60)]
        unit_before = {"Result": "success", "ExecMainStatus": "0", "ActiveState": "inactive",
                       "LastTriggerUSec": f"@{int(self.NOW - 54 * 3600)}",
                       "ExecMainExitTimestamp": f"@{int(self.NOW - 54 * 3600 + 600)}"}
        unit_after = {"Result": "exit-code", "ExecMainStatus": "1", "ActiveState": "inactive",
                      "LastTriggerUSec": f"@{int(self.NOW - 3600)}",
                      "ExecMainExitTimestamp": f"@{int(self.NOW - 60)}"}
        shows = iter([unit_before, {}, unit_after, {}])
        lines = []
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict(os.environ, {"PALACE_CHECKS_DIR": d, "PALACE_BACKUP_DIR": d}), \
                mock.patch.object(pbk, "dumps", side_effect=[before, after]), \
                mock.patch.object(pbk, "show", side_effect=lambda *a: dict(next(shows))), \
                mock.patch.object(pbk, "integrity_run", return_value=None), \
                mock.patch.object(pbk.subprocess, "run", return_value=mock.Mock(stdout="active\n")), \
                mock.patch.object(pbk.dt, "datetime", wraps=dt.datetime) as fake_dt, \
                mock.patch.object(pbk, "emit", side_effect=lambda _n, text: lines.append(text)):
            fake_dt.now.return_value = dt.datetime.fromtimestamp(self.NOW)
            pbk.body(True)
        self.assertEqual(len(lines), 1)
        self.assertIn("newest 20261120", lines[0])
        self.assertIn("last run failed", lines[0])

    def test_a_timer_that_stopped_triggering(self):
        old = self.NOW - 60 * 3600
        unit = self.unit(LastTriggerUSec=f"@{int(old)}", ExecMainExitTimestamp=f"@{int(old + 600)}")
        self.assertEqual(self._assess([4e9] * 3, unit=unit, newest_mtime=old + 600)[0], "fail")

    def test_shrinking_warns_then_fails(self):
        self.assertEqual(self._assess([4e9, 4e9, 3.7e9])[0], "warn")
        self.assertEqual(self._assess([4e9, 4e9, 3.0e9])[0], "fail")

    def test_a_tiny_dump_fails(self):
        self.assertEqual(self._assess([5e6, 5e6, 5e6])[0], "fail")

    def test_a_failed_unit_run_fails(self):
        state, fact = self._assess([4e9] * 3, unit=self.unit(Result="exit-code", ExecMainStatus="1"))
        self.assertEqual(state, "fail")
        self.assertIn("last run failed", fact)

    def test_few_dumps_warn(self):
        self.assertEqual(self._assess([4e9, 4e9])[0], "warn")

    def test_integrity_is_bound_to_the_dump_it_read(self):
        # review r1 (2): the recorded dump was truncated or rewritten since its ok
        state, fact = self._assess([4e9] * 3, current=(1_000_000, self.TRIG))
        self.assertEqual(state, "fail", fact)
        self.assertIn("changed since its integrity run", fact)

    def test_an_older_dumps_integrity_is_named_as_not_the_newest(self):
        found = self._dumps([4e9] * 3)
        rec = dict(self.good(found), dump=found[-2][0], size=found[-2][1], mtime=found[-2][2])
        state, fact = pbk.assess(found, self.NOW, self.unit(), "active", self.wrap(rec), (found[-2][1], found[-2][2]))
        self.assertEqual(state, "ok", fact)
        self.assertIn("(not the newest)", fact)

    def test_a_killed_integrity_run_does_not_leave_an_old_ok_standing(self):
        # review r1 (4): the run marks running_since first; killed, that mark stays and fails the line
        found = self._dumps([4e9] * 3)
        rec = self.wrap(self.good(found), running_since=self.NOW - 3 * 3600)
        state, fact = self._assess([4e9] * 3, integrity=rec)
        self.assertEqual(state, "fail", fact)
        self.assertIn("never finished", fact)
        rec["running_since"] = self.NOW - 600
        state, fact = self._assess([4e9] * 3, integrity=rec)
        self.assertEqual(state, "ok", fact)
        self.assertIn("integrity re-run in progress since", fact)

    def test_a_rerun_in_progress_never_hides_a_recorded_failure(self):
        # review r3: a failed run, then a re-run starts: the daily line still fails on the last completed result
        found = self._dumps([4e9] * 3)
        failed = dict(self.good(found), result="fail", why="gzip exit 1")
        state, fact = self._assess([4e9] * 3, integrity=self.wrap(failed, running_since=self.NOW - 300))
        self.assertEqual(state, "fail", fact)
        self.assertIn("the last integrity run failed", fact)
        self.assertIn("re-run in progress", fact)

    def test_starting_a_run_keeps_the_last_completed_result(self):
        # review r3: integrity_run must not overwrite last_completed before its own run completes
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PALACE_CHECKS_DIR": d}):
            pbk.write_integrity({"last_completed": {"result": "fail", "dump": "mempalace-20261119.sql.gz"},
                                 "running_since": None})
            seen = []

            def look(_path, opener=None):
                seen.append(pbk.read_integrity())
                return "ok", "whole stream, closing line last"
            found = [("mempalace-20261120.sql.gz", 4e9, self.NOW - 3600)]
            with mock.patch.object(pbk, "integrity_of", side_effect=look):
                pbk.integrity_run(Path(d), found, {"ActiveState": "inactive"}, self.NOW)
            self.assertEqual(seen[0]["last_completed"]["result"], "fail")     # during the run
            self.assertEqual(seen[0]["running_since"], self.NOW)
            after = pbk.read_integrity()
            self.assertEqual(after["last_completed"]["result"], "ok")        # a completed run replaces it
            self.assertIsNone(after["running_since"])

    def test_integrity_missing_old_or_failed(self):
        self.assertEqual(self._assess([4e9] * 3, integrity=None)[0], "warn")
        self.assertEqual(self._assess([4e9] * 3, integrity={"running_since": self.NOW - 60})[0], "warn")
        found = self._dumps([4e9] * 3)
        old = dict(self.good(found), at=self.NOW - 10 * 86400)
        self.assertEqual(self._assess([4e9] * 3, integrity=old)[0], "warn")
        bad = dict(self.good(found), result="fail")
        self.assertEqual(self._assess([4e9] * 3, integrity=bad)[0], "fail")

    def test_the_integrity_run_defers_while_the_dump_runs_or_settles(self):
        # review r1 (3): no lock across the system dump unit, so the reader refuses a running or fresh dump
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"PALACE_CHECKS_DIR": d}):
            found = [("mempalace-20261120.sql.gz", 4e9, self.NOW - 3600)]
            why = pbk.integrity_run(Path(d), found, {"ActiveState": "active"}, self.NOW)
            self.assertIn("dump unit is running", why)
            found = [("mempalace-20261120.sql.gz", 4e9, self.NOW - 120)]
            self.assertIn("under 10 min old", pbk.integrity_run(Path(d), found, {"ActiveState": "inactive"}, self.NOW))
            self.assertIsNone(pbk.read_integrity())

    def _opener(self, data, rc=0):
        class Proc:
            def __init__(self):
                self.stdout = io.BytesIO(data)

            def wait(self):
                return rc
        return lambda _p: Proc()

    def test_integrity_needs_the_whole_stream_and_the_closing_line_last(self):
        whole = b"x" * (3 << 20) + b"\n--\n-- PostgreSQL database dump complete\n--\n\n"
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(whole))[0], "ok")
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(b"x" * (3 << 20)))[0], "fail")
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(whole, rc=1)), ("fail", "gzip exit 1"))
        # review r1 (5): the phrase inside the last 4 KB but not as the last line is not a finished dump
        inside = b"x" * (3 << 20) + b"\n-- PostgreSQL database dump complete\nINSERT INTO t VALUES (1);\n"
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(inside))[0], "fail")
        quoted = b"x" * (3 << 20) + b"\nCOPY x FROM stdin;\nnote: PostgreSQL database dump complete\n"
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(quoted))[0], "fail")
        # pg_dump >= 17.6 (the real dumps, 11-20): one \unrestrict line follows the closing comment
        restricted = whole + b"\\unrestrict Zx9kQ2\n\n"
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(restricted))[0], "ok")
        twice = whole + b"\\unrestrict a\n\\unrestrict b\n"
        self.assertEqual(pbk.integrity_of(Path("d"), self._opener(twice))[0], "fail")

    def test_only_dump_names_count_and_oldest_first(self):
        with tempfile.TemporaryDirectory() as d:
            for n in ("mempalace-20261120.sql.gz", "mempalace-20261118.sql.gz", ".mempalace-20261121.sql.gz.tmp",
                      "other.sql.gz"):
                Path(d, n).write_bytes(b"x")
            self.assertEqual([x[0] for x in pbk.dumps(Path(d))],
                             ["mempalace-20261118.sql.gz", "mempalace-20261120.sql.gz"])


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConn:
    """Records every statement; the 30 s role timeout cancels a VACUUM unless this session lifted it."""

    def __init__(self, reloptions=None, dead=7):
        self.sql, self.timeout_off, self.reloptions, self.dead = [], False, reloptions, dead

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql.append(sql)
        if sql == "SET statement_timeout = 0":
            self.timeout_off = True
        if sql.startswith("VACUUM") and not self.timeout_off:
            raise RuntimeError("canceling statement due to statement timeout")
        if sql == "SHOW statement_timeout":
            return _FakeCursor(("0",) if self.timeout_off else ("30s",))
        return _FakeCursor((self.dead, self.reloptions))


class PalaceVacuum(unittest.TestCase):
    """palace-vacuum: the session lifts the role's statement_timeout before any VACUUM (2026-11-21)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        p = mock.patch.dict(os.environ, {"PALACE_CHECKS_DIR": str(self.tmp)})
        p.start()
        self.addCleanup(p.stop)

    def _run(self, argv, conn):
        with contextlib.redirect_stdout(io.StringIO()):
            pvac.main(argv, connect=lambda: conn)
        return (self.tmp / "palace-vacuum.line").read_text().strip()

    def test_the_timeout_is_lifted_first_and_only_with_set(self):
        sql = pvac.statements(["mempalace_kg.MENTIONS"])
        self.assertEqual(sql[0], "SET statement_timeout = 0")
        self.assertTrue(sql[1].startswith("SET lock_timeout"))
        self.assertEqual(sql[2], 'VACUUM (ANALYZE) "mempalace_kg"."MENTIONS"')
        self.assertFalse(any("ALTER ROLE" in s.upper() for s in sql))

    def test_a_vacuum_runs_past_the_role_timeout(self):
        conn = _FakeConn()
        out = self._run(["mempalace_drawers", "mempalace_kg.Drawer"], conn)
        self.assertTrue(out.startswith("CHECK palace-vacuum ok"), out)
        vac = [i for i, s in enumerate(conn.sql) if s.startswith("VACUUM")]
        self.assertEqual(len(vac), 2)
        self.assertLess(conn.sql.index("SET statement_timeout = 0"), vac[0])

    def test_autovacuum_left_off_is_a_warning(self):
        out = self._run([], _FakeConn(reloptions=["autovacuum_enabled=false"]))
        self.assertTrue(out.startswith("CHECK palace-vacuum warn"), out)
        self.assertIn("autovacuum OFF on public.mempalace_drawers", out)

    def test_a_name_that_is_not_a_plain_identifier_never_reaches_postgres(self):
        conn = _FakeConn()
        out = self._run(['mempalace_drawers"; DROP TABLE x; --'], conn)
        self.assertTrue(out.startswith("CHECK palace-vacuum fail"), out)
        self.assertEqual(conn.sql, [])
        for bad in ("a.b.c", "", "1abc", "sch.ta-ble"):
            with self.assertRaises(ValueError):
                pvac.parse_table(bad)

    def test_a_session_where_the_timeout_did_not_lift_fails_before_any_vacuum(self):
        class Ignored(_FakeConn):
            def execute(self, sql, params=None):
                if sql == "SET statement_timeout = 0":
                    self.sql.append(sql)  # accepted, but a pooler reset it
                    return _FakeCursor(None)
                return super().execute(sql, params)

        conn = Ignored()
        out = self._run([], conn)
        self.assertTrue(out.startswith("CHECK palace-vacuum fail · RuntimeError"), out)
        self.assertFalse(any(s.startswith("VACUUM") for s in conn.sql))

    def test_postgres_raising_is_a_fail_line_with_no_text_from_it(self):
        class Boom(_FakeConn):
            def execute(self, sql, params=None):
                raise RuntimeError("secret-looking detail")

        out = self._run([], Boom())
        self.assertTrue(out.startswith("CHECK palace-vacuum fail · RuntimeError"), out)
        self.assertNotIn("secret-looking", out)
