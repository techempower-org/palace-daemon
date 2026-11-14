"""The palace lane's checks (scripts/checks): one line each, judged from fictional fixtures."""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

import pytest

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


# --- checklib


def test_a_line_has_the_fixed_shape_and_refuses_an_unknown_state(tmp_path, monkeypatch):
    import re

    text = checklib.line("x-y", "warn", "3 things at 09:00")
    assert re.match(SHAPE, text)
    with pytest.raises(ValueError):
        checklib.line("x", "bad", "f")
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(tmp_path / "c"))
    checklib.emit("x-y", text)
    assert (tmp_path / "c" / "x-y.line").read_text() == text + "\n"


# --- dream-night


def test_dream_ok_when_the_night_published_and_the_judge_answered():
    out = dream.assess([_night("2026-11-15", 5, GOOD)], RAN, NOW)
    assert (
        out.startswith("CHECK dream-night ok")
        and "exit 0" in out
        and "60 cached" in out
    )


def test_dream_fails_when_nothing_was_published_or_the_night_is_stale():
    assert dream.assess([], RAN, NOW).startswith("CHECK dream-night fail")
    stale = dream.assess([_night("2026-11-13", 30, GOOD)], RAN, NOW)
    assert stale.startswith("CHECK dream-night fail") and "older than" in stale


def test_dream_warns_on_a_bad_exit_or_a_judge_error():
    bad = dream.assess(
        [_night("2026-11-15", 5, GOOD)], {**RAN, "ExecMainStatus": "3"}, NOW
    )
    assert (
        bad.startswith("CHECK dream-night warn")
        and "exit 3" in bad
        and "last run" in bad
    )
    err = dream.assess(
        [_night("2026-11-15", 5, {**GOOD, "error": "TimeoutError: busy"})], RAN, NOW
    )
    assert err.startswith("CHECK dream-night warn") and "judge error" in err


def test_dream_warns_only_after_three_nights_with_the_judge_off():
    two = [
        _night("2026-11-15", 5, OFF),
        _night("2026-11-14", 29, OFF),
        _night("2026-11-13", 53, GOOD),
    ]
    assert dream.assess(two, RAN, NOW).startswith("CHECK dream-night ok")
    three = [
        _night("2026-11-15", 5, OFF),
        _night("2026-11-14", 29, OFF),
        _night("2026-11-13", 53, OFF),
    ]
    out = dream.assess(three, RAN, NOW)
    assert out.startswith("CHECK dream-night warn") and "judge off 3 nights" in out


def test_dream_reads_only_day_files_newest_first(tmp_path):
    for day in ("2026-11-13", "2026-11-15", "2026-11-14"):
        (tmp_path / f"{day}.json").write_text(json.dumps({"day": day}))
    (tmp_path / "latest.json").write_text("{}")
    (tmp_path / "2026-11-16.json").write_text("{broken")
    nights = dream.read_nights(tmp_path)
    assert nights[0] is dream.UNREADABLE  # the corrupt newest is kept, not skipped
    assert [n["day"] for n in nights[1:]] == ["2026-11-15", "2026-11-14"]
    out = dream.assess(nights, RAN, NOW)
    assert out.startswith(
        "CHECK dream-night fail · the newest published night is unreadable"
    )


# --- secret-drawers


def test_a_hash_is_new_only_when_its_first_drawer_anywhere_is_in_the_window():
    since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
    rows = [
        ("aws", "aaaa0000", "w1", "2026-11-14T10:00:00Z"),  # new
        ("aws", "aaaa0000", "w2", "2026-11-14T11:00:00Z"),  # same secret, a second wing
        ("jwt", "bbbb1111", "w1", "2026-11-14T10:00:00Z"),  # seen again today ...
        (
            "jwt",
            "bbbb1111",
            "w3",
            "2026-10-01T10:00:00Z",
        ),  # ... but first held in October
    ]
    assert secrets.new_secrets(rows, since) == [("aws", ["w1", "w2"])]


def test_raw_text_after_the_rules_loaded_fails_and_older_is_a_sweep_candidate():
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


def test_secret_lines_fail_warn_and_ok_never_print_a_value():
    since = NOW
    fail = secrets.assess([], {("w1", "aws"): 2}, {}, 0, 10, since)
    assert fail.startswith("CHECK secret-drawers fail") and "w1 (aws×2)" in fail
    warn = secrets.assess(
        [("jwt", ["w2", "w3"])], {}, {("w4", "litellm"): 1}, 5, 10, since
    )
    assert warn.startswith("CHECK secret-drawers warn")
    assert (
        "1 secret(s) first mined" in warn
        and "w2 (jwt×1)" in warn
        and "w4 (litellm×1)" in warn
    )
    ok = secrets.assess([], {}, {}, 7, 10, since)
    assert ok.startswith("CHECK secret-drawers ok") and "7 unhashed" in ok


def test_filed_reads_utc_and_naive_local_stamps():
    assert secrets.filed("2026-11-14T15:17:39.103Z").utcoffset() == dt.timedelta(0)
    assert secrets.filed("2026-11-14T08:05:45.109123").tzinfo is not None
    assert secrets.filed("") is None and secrets.filed("nope") is None


# --- gs-auth


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


def test_refusals_right_after_a_start_are_deploy_probes_not_callers():
    ev = [[100.0, "start"], [100.5, "refused"], [130.0, "refused"], [500.0, "refused"]]
    callers, probes, last = gs.caller_refusals(ev)
    assert (callers, probes, last) == (1, 2, 500.0)
    assert gs.caller_refusals([[5.0, "refused"]])[:2] == (
        1,
        0,
    )  # no start in the window


def test_gs_ok_when_each_host_is_as_expected():
    out = gs.assess(
        [
            ("h1", "required", _host(True, [[1.0, "start"], [2.0, "refused"]])),
            ("h2", "open", _host(False)),
        ],
        NOW,
    )
    assert out.startswith("CHECK gs-auth ok") and "1 deploy probe(s)" in out


def test_gs_warns_on_a_refused_caller_a_wrong_state_or_an_old_service():
    caller = gs.assess([("h1", "required", _host(True, [[900.0, "refused"]]))], NOW)
    assert caller.startswith("CHECK gs-auth warn") and "a caller is refused" in caller
    flipped = gs.assess([("h1", "required", _host(False))], NOW)
    assert flipped.startswith("CHECK gs-auth warn") and "expected required" in flipped
    old = gs.assess([("h2", "open", _host(False, bwfix=0))], NOW)
    assert old.startswith("CHECK gs-auth warn") and "boot-PATH" in old


def test_gs_fails_when_a_host_is_down_unreachable_or_unreadable():
    assert gs.assess([("h1", "open", None)], NOW).startswith("CHECK gs-auth fail")
    assert gs.assess([("h1", "open", _host(active="failed"))], NOW).startswith(
        "CHECK gs-auth fail"
    )
    unreadable = {"active": "active", "status": None, "events": [], "bwfix": 1}
    assert gs.assess([("h1", "open", unreadable)], NOW).startswith("CHECK gs-auth fail")
    assert gs.assess([], NOW).startswith("CHECK gs-auth fail")


def test_the_worst_host_decides_and_every_host_is_named():
    out = gs.assess([("h1", "open", _host(False)), ("h2", "open", None)], NOW)
    assert (
        out.startswith("CHECK gs-auth fail")
        and "h1 open" in out
        and "h2 unreachable" in out
    )


def test_host_specs_are_parsed_and_bad_items_reported():
    hosts, bad = gs.expectations("a=required b=open c=maybe =open d")
    assert hosts == [("a", "required"), ("b", "open")] and bad == [
        "c=maybe",
        "=open",
        "d",
    ]


def test_no_host_name_or_private_address_is_written_into_the_checks():
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
                    h for h, _ in gs.expectations(row.split("=", 1)[1].strip('"'))[0]
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


def test_dream_fails_cleanly_on_an_unreadable_night_or_a_missing_dir(
    tmp_path, monkeypatch, capsys
):
    bad = dream.assess([{"day": "2026-11-15", "run": {}}], RAN, NOW)
    assert bad.startswith("CHECK dream-night fail") and "generated_at" in bad
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(tmp_path / "c"))
    monkeypatch.setenv("DREAM_OUT", str(tmp_path / "nowhere"))
    assert dream.main() == 0
    assert capsys.readouterr().out.startswith(
        "CHECK dream-night fail · DREAM_OUT is not a directory"
    )


# --- review round 1 (xreview): fail closed, never tokenless, never leak


def test_gs_never_probes_without_a_token_config_and_says_so():
    out = gs.assess([("h1", "required", _host(tokenconf=False))], NOW)
    assert out.startswith("CHECK gs-auth fail") and "never tokenless" in out


def test_gs_warns_when_the_journal_could_not_be_read():
    out = gs.assess([("h1", "required", _host(journal=False))], NOW)
    assert out.startswith("CHECK gs-auth warn") and "journal unreadable" in out


def test_refusals_before_the_window_are_dropped_but_their_start_still_matches_a_probe():
    day = 24 * 3600
    now = 200_000.0
    start = now - day
    ev = [[start - 30, "start"], [start + 10, "refused"], [start - 5, "refused"]]
    assert gs.caller_refusals(ev, start) == (0, 1, None)


def test_host_specs_reject_ssh_options_and_odd_names():
    hosts, bad = gs.expectations("-F/tmp/x=required ok-host=open a;b=open .x=open")
    assert hosts == [("ok-host", "open")] and len(bad) == 3
    assert gs.read_host("-oProxyCommand=x") is None


def test_dream_warns_when_the_unit_state_is_unknown_or_the_time_is_in_the_future():
    unknown = dream.assess([_night("2026-11-15", 5, GOOD)], {}, NOW)
    assert (
        unknown.startswith("CHECK dream-night warn")
        and "unit state unreadable" in unknown
    )
    future = dream.assess([_night("2026-11-15", -3, GOOD)], RAN, NOW)
    assert future.startswith("CHECK dream-night warn") and "future" in future
    naive = _night("2026-11-15", 5, GOOD)
    naive["generated_at"] = "2026-11-15T04:00:00"
    assert dream.assess([naive], RAN, NOW).startswith("CHECK dream-night fail")


def test_a_judge_error_shows_its_type_never_its_text():
    leak = {**GOOD, "error": "URLError: http://user:hunter2-secret@host/x"}
    out = dream.assess([_night("2026-11-15", 5, leak)], RAN, NOW)
    assert "URLError" in out and "hunter2" not in out


def test_lines_are_one_line_bounded_and_labels_are_plain():
    text = checklib.line("x", "ok", "a\nb\r\nc " + "z" * 2000, "n\nx")
    assert "\n" not in text and "\r" not in text and len(text) < 1000
    assert checklib.label("wing\nname; rm -rf") == "wing?name??rm?-rf"
    out = secrets.assess([("jwt", ["evil\nwing"])], {}, {}, 0, 1, NOW)
    assert "\n" not in out and "evil?wing" in out


def test_a_check_body_that_raises_becomes_a_fail_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(tmp_path))

    def boom():
        raise KeyError("x")

    assert checklib.run("t-x", boom) == 0
    assert capsys.readouterr().out.startswith(
        "CHECK t-x fail · the check itself raised KeyError"
    )


def test_an_unwritable_line_dir_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(blocker / "sub"))
    checklib.emit("t-y", checklib.line("t-y", "ok", "f"))
    assert "line not saved" in capsys.readouterr().out


def test_read_var_reads_plain_and_export_forms_quietly(tmp_path):
    f = tmp_path / "env"
    f.write_text('# c\nexport A=x1\nB="y 2"\nC=\n')
    assert checklib.read_var(f, "A") == "x1" and checklib.read_var(f, "B") == "y 2"
    assert (
        checklib.read_var(f, "C") == ""
        and checklib.read_var(tmp_path / "none", "A") == ""
    )


def test_a_hash_with_no_readable_stamp_is_not_called_new():
    since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
    rows = [
        ("aws", "cccc2222", "w1", "garbage"),
        ("aws", "dddd3333", "w1", "2026-11-14T08:59:00+00:00"),
        ("aws", "dddd3333", "w2", "2026-11-14T00:59:00"),
    ]  # naive local: earlier than 08:59Z
    assert secrets.new_secrets(rows, since) == []


# --- review round 2


def test_a_misspelt_host_state_fails_the_check_instead_of_dropping_the_host(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(tmp_path))
    monkeypatch.setenv("GS_CHECK_HOSTS", "a=required b=requird")
    monkeypatch.setattr(gs, "read_host", lambda h: pytest.fail("no host may be read"))
    assert gs.main() == 0
    out = capsys.readouterr().out
    assert (
        out.startswith("CHECK gs-auth fail")
        and "b?requird" in out
        and out.count("\n") == 1
    )


def test_an_unsaved_line_is_still_one_line(tmp_path, monkeypatch, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(blocker / "sub"))
    checklib.emit("t-z", checklib.line("t-z", "ok", "f"))
    out = capsys.readouterr().out
    assert (
        out.count("\n") == 1
        and "line not saved" in out
        and out.startswith("CHECK t-z ok")
    )


def test_a_unit_that_is_not_loaded_is_not_trusted():
    out = dream.assess(
        [_night("2026-11-15", 5, GOOD)], {**RAN, "LoadState": "not-found"}, NOW
    )
    assert out.startswith("CHECK dream-night warn") and "not loaded" in out


@pytest.mark.parametrize(
    "err, shown",
    [
        ("URLError: <urlopen error refused>", "URLError"),
        ("socket.timeout: timed out", "socket.timeout"),
        ("password=hunter2 in the prompt", "an error"),
        ("TimeoutError", "TimeoutError"),
        (
            "hunter2Error: failed",
            "an error",
        ),  # an Error-shaped secret is not a known type
        ("stopped: low memory", "stopped"),
    ],
)
def test_a_judge_error_is_shown_as_a_type_or_a_fixed_marker(err, shown):
    out = dream.assess([_night("2026-11-15", 5, {**GOOD, "error": err})], RAN, NOW)
    assert f"judge error ({shown})" in out and "hunter2" not in out


def test_judge_counts_that_are_not_numbers_are_not_printed():
    con = {**OFF, "waiting": "60; rm -rf", "cached": -1}
    assert "off (? waiting, ? cached)" in dream.judge_state(con)


def test_the_installer_creates_the_writable_dir_and_checks_the_configured_one():
    text = (CHECKS.parent / "systemd" / "install-checks.sh").read_text()
    assert 'mkdir -p "$lines"' in text and "PALACE_CHECKS_DIR must be" in text
    for unit in (CHECKS.parent / "systemd").glob("*.service"):
        body = unit.read_text()
        if "ReadWritePaths" in body:
            assert "ReadWritePaths=%h/Projects/palace-daemon/scratch/checks" in body


def test_the_remote_takes_only_the_token_header_and_never_runs_the_host_curl_config():
    remote = gs.REMOTE
    assert '-K "$conf"' not in remote and "-K -" in remote and "curl -q" in remote


def test_the_dream_check_asks_systemd_for_every_property_it_judges(
    tmp_path, monkeypatch, capsys
):
    (tmp_path / "2026-11-15.json").write_text(json.dumps(_night("2026-11-15", 1, GOOD)))
    monkeypatch.setenv("DREAM_OUT", str(tmp_path))
    monkeypatch.setenv("PALACE_CHECKS_DIR", str(tmp_path / "c"))
    seen = {}

    class Done:
        returncode = 0
        stdout = "LoadState=loaded\nResult=success\nExecMainStatus=0\nExecMainExitTimestamp=Sun x\n"

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(dream.subprocess, "run", fake_run)
    monkeypatch.setattr(
        dream.dt,
        "datetime",
        type("D", (dt.datetime,), {"now": staticmethod(lambda tz=None: NOW)}),
    )
    assert dream.main() == 0
    props = {seen["argv"][i + 1] for i, a in enumerate(seen["argv"]) if a == "-p"}
    assert {"LoadState", "Result", "ExecMainStatus", "ExecMainExitTimestamp"} <= props
    assert capsys.readouterr().out.startswith("CHECK dream-night ok")


def test_known_public_hashes_are_ignored_and_the_list_takes_only_kind_sha8():
    since = dt.datetime(2026, 11, 14, 9, 0, tzinfo=dt.timezone.utc)
    rows = [
        ("google", "34d4aaaa", "w1", "2026-11-14T10:00:00Z"),
        ("aws", "aaaa0000", "w1", "2026-11-14T10:00:00Z"),
    ]
    skip = secrets.ignored("google:34d4aaaa AIzaSyNOTAHASH aws:XYZ bad")
    assert skip == {("google", "34d4aaaa")}
    assert secrets.new_secrets(rows, since, skip) == [("aws", ["w1"])]
