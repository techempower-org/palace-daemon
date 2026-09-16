# Using the TV as a monitor on 0lympus — quick reference

Cable stays **plugged into DP-1**. Background DRM polling is disabled
(`drm_kms_helper.poll=0`) because it was what froze the box — see memory note
`olympus-display-outage-2026-07-15`.

## Confirmed behaviour (2026-07-22 test)

- The active DP→HDMI adapter is **TV-powered**: TV off → DP-1 reads `disconnected`.
- Turning the TV on sends **no hotplug signal** the box acts on (polling is off).
- So the TV does **not** come up on its own — you run one command.

## ⚠️ 2026-09-16: powering the TV off while DP-1 is lit FREEZES THE BOX

The TV keeps the adapter powered for a few minutes after the remote's "off". When it
finally cuts, the DP link drops, i915 probes the dying link and the box hard-hangs
(journal just stops; the iTCO watchdog does not recover it; manual power-cycle needed).
`poll=0` does not protect against this — it is a hotplug-triggered wedge, not the poll timer.

**Rule: force DP-1 off BEFORE you power the TV off.** The stick button now does that.

## Everyday routine (the button is a TOGGLE)

- **TV on:** turn the TV on, **long-press the side button (KEY2) on the admin voice
  stick** (≥1.2s). DP-1 is dark → it runs `tv-on` → login screen appears.
- **TV off:** **long-press KEY2 first** (DP-1 is lit → it runs `tv-off`, screen blanks),
  **then** press the TV remote's off.
- Over SSH the same two commands are `tv-on` and `tv-off`.

The long-press hits `tv-trigger.service` on the host (LAN `:8099`, token-gated, 15s
cooldown). It keys the direction on `/sys/class/drm/card*-DP-1/status`: `connected` →
`tv-off`, otherwise `tv-on`. Endpoints: `/tv-on`, `/tv-wake`, `/tv-toggle` all toggle;
`/tv-force-on` and `/tv-force-off` are explicit. Short-press KEY2 still = "replay". The
service and token live in `~/tv-trigger/`; disable anytime with
`sudo systemctl disable --now tv-trigger`. Verified 2026-09-16: off→on round-trip works
with a session running, no GDM restart needed.

`tv-on` does it all in one shot: a single safe DP-1 probe, then (if the TV is present
but not yet driven) a GDM re-enumerate that actually lights it. Expect to run `tv-on`
each time the TV has been off for a while — that's normal for this adapter, not a bug.

## Commands

| Command | What it does |
|---|---|
| `tv-on` | Detect the TV and light it (one command). Safe over SSH. |
| `tv-on --force` | Same, even if a local session is active (it gets logged out). |
| `tv-off` | Force DP-1 off. **Run before powering the TV off** (else freeze). Undo with `tv-on`. |
| `tv-detect` | Root engine the others call. **Never run directly.** |

Add `-h` to `tv-on`/`tv-off` for inline help.

## Notes / gotchas

- Kernel 7.0 renamed the GPU node `card0` → `card1`; all three scripts now glob
  `card*-DP-1` (they silently did nothing between the kernel upgrade and 2026-09-16).
- `tv-on` guards against logging out an active local session — it stops and tells you to
  use `--force` if someone's using the TV locally.
- SSH and xrdp sessions are **separate** and never affected by `tv-on`.
- If `tv-on` says "no TV detected", the TV is off or the cable/link dropped — power the
  TV on and run it again.
