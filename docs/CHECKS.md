# CHECKS: the palace lane's durable checks (systemd user timers)

JP 2026-11-14 10:1x: "we should have more crons checking things clearly", "i keep running into issues where data is stale or static". Rule (Sen, 11/14): durable systemd user timers, never session crons. Each check prints ONE line in the fixed shape

    CHECK <name> <ok|warn|fail> · <fact, number, time> · <next/owner>

to stdout (the unit's journal) and to `$PALACE_CHECKS_DIR/<name>.line`, replaced atomically. A check reports, it never gates: every script exits 0 and its state is in the line. warn/fail go in the lane's daily line the same morning.

Scripts: `scripts/checks/` (pure functions, tested in `tests/test_palace_checks.py` on fictional fixtures; a body that raises still prints a fail line). Units: `scripts/systemd/palace-check-*.{service,timer}`, installed by `scripts/systemd/install-checks.sh`. Every unit:

- sets `Environment=PATH=…` (a user unit started at boot sees a bare PATH);
- loads only `~/.config/palace-checks/env`, which holds paths and hosts, never a secrets file. On 2026-11-14 a trial pointed `EnvironmentFile=` at a file with `export` lines, and systemd logged the whole rejected line, password included, to the journal. secret-drawers reads just the DSN itself (`PALACE_CHECKS_DSN_FILE`, default the daemon's env file) and opens a read-only session;
- runs with `NoNewPrivileges`, `UMask=0077`, no core dumps, at idle I/O. The two local checks also get a read-only filesystem with the line dir writable. gs-auth doesn't: in a user unit those options run in a user namespace, and ssh then refuses `/etc/ssh` as "bad owner" (measured).

Host names and local paths for the data live in `~/.config/palace-checks/env` on the host that runs the checks, not in this repo. A test checks the scripts, units, installer and this file for the configured names and for private addresses; CI has no config, so it checks the addresses only. The units do assume the install layout (`~/Projects/palace-daemon`, the daemon venv).

    DREAM_OUT=<dir with the published dream nights>
    GS_CHECK_HOSTS="<host>=required <host>=open"     # expected token state per desktop
    PALACE_CHECKS_DIR=<this repo>/scratch/checks       # the units' only writable path
    # PALACE_CHECKS_DSN_FILE=<env file with MEMPALACE_POSTGRES_DSN>  (optional)

| name | timer | cadence | measures | line shape | owner | the miss behind it |
|---|---|---|---|---|---|---|
| dream-night | palace-check-dream-night.timer | daily 04:20 | the newest published dream night (`DREAM_OUT/YYYY-MM-DD.json`), palace-dream.service's last exit, the contradictions judge over the last 3 nights | `CHECK dream-night ok · <day> published <time> (<h> ago), exit 0, <n> proposals, judge <judged/cached \| off (w waiting)>`; **fail** if nothing published or the newest is older than 27 h; **warn** if the exit ≠ 0, the judge recorded an error, or it was off 3 nights running | palace lane | 11/14: the judge had been off for nights with 60 pairs waiting; nobody saw it until JP asked why Dreams never showed a contradiction |
| secret-drawers | palace-check-secret-drawers.timer | daily 04:35 | over all drawers, shared and private: hashed redaction placeholders whose first appearance anywhere is in the last 24 h (a new secret printed into a session), and raw text the current rules would still redact in drawers filed in the last 24 h | **fail** `· n raw secret-shaped run(s) … <wing> (<kind>×n)` (after the rules last loaded: a writer skipped redaction); **warn** `· n new secret(s) first seen in the palace: <wing> (<kind>×n)` or raw runs in drawers older than the rules (sweep candidates); **ok** `· 0 new secrets, 0 raw …; n unhashed redactions`. Wings and kinds only, never a value or hash | the palace lane for raw text and sweeps; the wing's owner rotates a new secret | 11/14: a LiteLLM key fragment printed into a transcript was found by a person reading the transcript |
| gs-auth | palace-check-gs-auth.timer | daily 07:50, 15:50 | on each `GS_CHECK_HOSTS` desktop over ssh: gnome-speaks active, `/status` auth read with the host's own token (never a tokenless request), "refused:" lines over 24 h (those within 60 s of a service start are the deploy's own probes, counted apart), and whether the deployed service finds `bw` beyond PATH | `CHECK gs-auth ok · <host> required, 0 caller(s) refused/24h, k deploy probe(s); <host> open, …`; **fail** if a host is unreachable, the unit is not active or /status is unreadable; **warn** if a caller is refused, the token state differs from the expected one, or the service predates the boot-PATH fix | palace lane (gnome-speaks deploys) | 11/13: after the token flip, a caller the audit missed would have been refused silently; 11/14: a desktop's unit started at boot could not find `bw` |

## Adding a check

One script under `scripts/checks/`, pure functions over text so the test can feed fictional fixtures; one `.service` + `.timer` pair in `scripts/systemd/` with the `Environment=PATH=…` line; a row here in the same PR; then `scripts/systemd/install-checks.sh`. A warn that needs JP's click names the owner who files the card. The script never files one.
