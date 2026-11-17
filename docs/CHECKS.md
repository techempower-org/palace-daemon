# CHECKS: the palace lane's durable checks (systemd user timers)

JP 2026-11-14 10:1x: "we should have more crons checking things clearly", "i keep running into issues where data is stale or static". These follow plan.realm.watch FOR-AGENTS §24. Each check is a systemd user timer that writes ONE line,

    CHECK <name> <ok|warn|fail> · <fact> · <next>

to `~/.local/state/checks/<name>.line` on the host that runs it (`<name>.line.tmp`, then rename) and to its unit's journal. The plan's Hearth › Checks reads that file, and the 08:11 pass reads the warn/fail set. `ok` = it ran and found nothing; `warn` = it found something a lane will handle; `fail` = it couldn't run or couldn't read what it measures (never a quiet zero). A check reports, never gates: every script exits 0, even when its body raises.

This repo is public, so **no host name appears here or in a line**: `host` names the role (plan-62 11-14: a public repo's host may be a role), facts carry counts and names of things, and the desktops are shown by the label each is given in the local config.

| name | host | timer | cadence | measures | owner | the miss behind it | line shape |
|---|---|---|---|---|---|---|---|
| palace-dream-night | palace host | palace-check-dream-night.timer | daily 04:20 | the newest published dream night (`$DREAM_OUT/YYYY-MM-DD.json`: generated_at, proposals, contradictions judge), palace-dream.service (LoadState, Result, exit, and a start skipped by its ExecCondition: warn while no night is newer than the skip), judge state over the last 3 nights | palace-nemotron | 11-14: the dream judge had been off for nights with 60 pairs waiting; nobody saw it until JP asked why Dreams never showed a contradiction. 11-16/17: the MemAvailable ExecCondition skipped two 03:30 starts and the line read ok ("no run since boot") until the night went stale | `ok · <day> published <time> (<h> ago), exit 0 \| no run since boot \| last start <time> skipped by its start condition, <n> proposals, judge <n judged, n cached \| off (n waiting)>`; **fail** nothing published, newest unreadable, or older than 27 h; **warn** unit unreadable/not loaded, last start skipped by its start condition with no night since (or its time unreadable), last run failed, a judge error (its type only), judge off 3 nights, a future stamp |
| palace-secret-drawers | palace host | palace-check-secret-drawers.timer | daily 04:35 | every drawer, shared and private: hashed redaction placeholders whose first appearance in the palace is in the last 24 h; text the current rules would still redact in drawers filed in the last 24 h | palace-nemotron (raw text, sweeps); each wing's owner (rotation) | 11-14: a LiteLLM key fragment printed into a transcript was found by a person reading it | **fail** `n raw secret-shaped run(s) … <wing> (<kind>×n)` (filed after the rules last loaded); **warn** `n secret(s) first mined into the palace (not necessarily printed today): <wing> (<kind>×n)`, or raw runs in older drawers (sweep candidates); **ok** `0 new secrets, 0 raw …; n unhashed redactions`. Wings and kinds only; never a value or a hash |
| palace-gs-auth | palace host | palace-check-gs-auth.timer | every 8 h | each desktop in `GS_CHECK_HOSTS`, over ssh: gnome-speaks active; `/status` auth read with the host's own token header (never a tokenless request, never the host's whole curl config); "refused:" journal lines over 24 h (within 60 s of a service start = the deploy's own probes, counted apart); the deployed service finds `bw` beyond PATH | palace-nemotron | 11-13: after the token flip a caller the audit missed would have been refused silently; 11-14: a desktop's unit started at boot could not find `bw` | `ok · <label> required, 0 caller(s) refused/24h, k deploy probe(s); <label> open, …`; **fail** unreachable, unit not active, no token config, /status unreadable, a bad `GS_CHECK_HOSTS` item (named by position only; a label equal to a host is refused); **warn** a caller refused, token state not the expected one, journal unreadable, service predates the boot-PATH fix |
| palace-redact-needles | palace host | palace-check-redact-needles.timer | every 10 min | a probe that loads mempalace.redact's patterns in process as a write does (it fetches each `secret_vault_items` needle through bw), and the daemon journal's last 20 min for writes refused with `SecretPatternsUnavailable` | palace-nemotron | 11-15: a password with no secret shape matched no rule and reached the palace; the needle that catches it (mempalace #619) fails every write closed when the vault locks, so mining can stop silently | **fail** `mining stopped: vault item <label> cannot load now …` or `n palace write(s) refused in the last 20 min (<label>)`; **warn** journal unreadable, or fewer needles than `REDACT_NEEDLES_EXPECT`; **ok** `n needle(s) loaded (<labels>); 0 palace writes refused`. Labels only, never a value |

## Running them

Scripts are in `scripts/checks/`: pure functions, tested in `tests/test_palace_checks.py` (unittest) on fictional fixtures. Units are in `scripts/systemd/palace-check-*.{service,timer}`, installed by `scripts/systemd/install-checks.sh`. Every unit:

- sets `Environment=PATH=…`, because a user unit started at boot sees a bare PATH;
- loads only `~/.config/palace-checks/env` (paths and hosts), never a secrets file. On 11-14 a trial pointed `EnvironmentFile=` at a file with `export` lines, and systemd logged the rejected line, password included, to the journal. secret-drawers reads just the DSN itself (`PALACE_CHECKS_DSN_FILE`, default the daemon's env file) and opens a read-only session;
- runs with `NoNewPrivileges`, `UMask=0077`, no core dumps, at idle I/O. The two local checks also get a read-only filesystem with `~/.local/state/checks` writable. gs-auth doesn't: in a user unit those options run in a user namespace, and ssh then refuses `/etc/ssh` as "bad owner" (measured).

Local config, on the host that runs the checks (not in this repo):

    DREAM_OUT=<dir with the published dream nights>
    GS_CHECK_HOSTS="<host>=required@<label> <host>=open@<label>"   # expected token state; the label is what the line shows
    PALACE_CHECKS_DIR=/home/<user>/.local/state/checks              # absolute: systemd does not expand $HOME here; the units' only writable path
    # PALACE_CHECKS_DSN_FILE=<env file with MEMPALACE_POSTGRES_DSN>  (optional)
    # SECRET_CHECK_IGNORE="<kind>:<sha8> …"   known-public values, by placeholder hash only

A test checks the scripts, units, installer and this file for the configured host names and for private addresses (CI has no config, so it checks the addresses only). The units assume the install layout: `~/Projects/palace-daemon` and the daemon's venv.

## Adding a check

Write one script under `scripts/checks/` and one `.service` + `.timer` pair with the `Environment=PATH=…` line. Add a row here in the same commit, then run `install-checks.sh`. A warn that needs JP's click names the owner who files the card; the script never files one.
