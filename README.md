# astrorainprotect

## What it does

astrorainprotect is a self-hosted rain alarm. Point it at a location, and it
watches NOAA MRMS radar (reflectivity and PrecipRate) around that `LAT,LON`,
keeps Pirate Weather as a secondary trigger for organized systems, and pushes a
notification through [ntfy](https://ntfy.sh) (iOS or Android app, or any ntfy
client) when rain is heading your way. It was built to give enough lead time to
bring in telescopes left outside overnight, but nothing in it is specific to
that: it is an alarm for anything you would rather not leave out in the rain.

It works anywhere inside MRMS coverage, which is the contiguous United States
(roughly 20–55 N, 130–60 W). All settings come from environment variables; the
only state it keeps is a small directory with the alert latch and a heartbeat.

## Quick start (Portainer)

1. On the Docker host, create the state directory and hand it to the container's
   user: `mkdir -p /opt/astrorainprotect/state && chown 1000:1000
   /opt/astrorainprotect/state`.
2. In Portainer, add a Git-repository stack pointing at this repo with compose
   path `portainer-stack.yml`.
3. In the stack's Environment variables section, set `LAT`, `LON`, `NTFY_URL`,
   and optionally `NTFY_TOKEN` (the rest have working defaults — see
   Configuration below).
4. Deploy the stack.

The GHCR package (`ghcr.io/ddovidenko/astrorainprotect`) must be set to public
in GitHub package settings, or Portainer needs registry credentials configured
for `ghcr.io`, before it can pull the image.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                       # unit tests, no network
.venv/bin/pytest -m integration        # also hits the live MRMS S3 bucket
cp .env.example .env && docker compose up --build
```

## Configuration

Legacy names preserved; new vars marked.

| Var | Default | Meaning |
|---|---|---|
| `LAT`, `LON` | required | house coordinates |
| `NTFY_URL` | required | server + topic |
| `NTFY_TOKEN` | empty | bearer token for protected topics |
| `NTFY_PRIORITY` (new) | `high` | ntfy priority header |
| `DEBUG` | 0 | 0/1/2 |
| `POLL_SEC` | 180 | radar poll interval |
| `ALERT_RADIUS_KM` | 20 | radius for "nearby" |
| `NOW_RADIUS_KM` | 1 | radius for "raining now" |
| `MIN_INTENSITY` | 0.2 | mm/h for a PrecipRate cell to count |
| `MIN_DBZ` (new) | 30 | dBZ for a reflectivity cell to count |
| `MIN_CELLS` | 3 | qualifying cells needed |
| `RAINING_NOW` | 0.05 | mm/h at the site = already raining |
| `DIRECTION_FILTER` | 0 | 1 = ignore echoes moving away |
| `LOOKAHEAD_MIN` | 60 | projection horizon, also Pirate Weather window |
| `REPEAT_MIN` | 0 | repeat interval while active |
| `SCOPE_HOSTS` | empty | scope-online gate |
| `PW_KEY` | empty | enables Pirate Weather trigger |
| `MIN_PROB` | 0.3 | Pirate Weather probability threshold |
| `REPLAY_DIR` (new) | empty | run detector over saved frames and exit |
| `STATE_DIR` (new) | `/state` | latch/heartbeat directory; tests and local runs override it |
| `TZ` | America/Chicago | log timestamps |

Startup validates required vars and numeric ranges, prints the effective config
with `NTFY_TOKEN` and `PW_KEY` masked, and exits non-zero on error.

## How alerts behave

Each poll cycle checks radar and, if configured, Pirate Weather, and feeds the
result into a small latch-based state machine (ported from the legacy shell
script):

- **First trigger**: if rain qualifies nearby and the alarm isn't already
  latched, it sends an alert and latches.
- **Latch**: while latched, further qualifying cycles are skipped — no repeat
  notifications — unless `REPEAT_MIN` is set.
- **Repeat**: with `REPEAT_MIN > 0`, a "still raining" repeat alert is sent once
  the latch has been active for at least that many minutes; with the default
  `REPEAT_MIN=0`, there is no repeat, ever.
- **Rain at the site**: if PrecipRate within `NOW_RADIUS_KM` reaches
  `RAINING_NOW` (`raining_now=1`), that is itself a trigger (source `house`,
  ETA 0). Unlatched, it sends a notification titled "Currently raining" with the
  body "Currently raining at the house (0.8 mm/h)", followed by any other active
  source; latched, it is skipped like any other trigger (or repeated under
  `REPEAT_MIN` as "Currently raining (still)"). This is a deliberate
  departure from the legacy script, which treated rain at the site as "nothing
  left to warn about" and silently re-armed: a cell that pops up directly
  overhead — in-place convection, the case this project exists for — would
  never have produced an alert. Pirate Weather's own "raining now" never
  silences a radar trigger.
- **Rearm**: once no trigger remains (nothing nearby, nothing overhead, no
  Pirate Weather ETA) the latch clears, so the next qualifying cell can alert
  again.
- **Scope gate**: if `SCOPE_HOSTS` is set, the cycle first checks whether any of
  those hosts are online (for example a smart telescope's JSON-RPC port). If none are, the latch is
  cleared and the cycle skips radar/Pirate Weather checks entirely for that
  poll — nothing outside is at risk to protect.
- **DEBUG levels**: `DEBUG=0` is silent apart from normal INFO logging.
  `DEBUG=1` adds extra detail lines (which scope hosts are online, the Pirate
  Weather summary). `DEBUG=2` additionally sends a one-time test notification
  on every container start, so you can confirm the ntfy path works without
  waiting for rain. The test is sent before the scope gate and says what the
  gate will do: "Scopes online: ...", "No scope online (...); waiting for one
  before checking radar." or "Scope gate disabled; checking every poll."
- **Direction filter**: with `DIRECTION_FILTER=1`, storm motion is estimated
  from the last few reflectivity frames and the nearest qualifying echo is
  projected along it. If its projected path does not come within
  `max(NOW_RADIUS_KM, ALERT_RADIUS_KM / 4)` of the site inside `LOOKAHEAD_MIN`,
  the radar trigger is dropped for that cycle (the summary line shows
  `note=moving away`). When it will hit, the alert carries an ETA, counted
  down by the age of the newest frame. When motion is unknown (too few
  frames, low confidence, sub-resolution shift), the filter never suppresses:
  plain radius alerting applies. Rain at the site always alerts.

## Reading the logs

Each poll cycle ends with one summary line, for example:

```
2026-09-23 14:32:07 INFO frame=19:31:00Z age=1.1min reflectivity=max:38.4,cells:5,nearest:12.3 km to the NW preciprate=max:0.0,cells:0,nearest:no echo in range raining_now=0 eta=none sources=radar latched=1 outcome=send note=-
```

- `frame` — UTC valid time of the newest radar frame used this cycle (`none` if
  none was available).
- `age` — how old that frame was when the cycle ran.
- per-product fields (`reflectivity=...`, `preciprate=...`) — for each radar
  product, `max` (highest value in the alert radius), `cells` (qualifying cell
  count), and `nearest` (distance and compass direction to the nearest
  qualifying cell, e.g. `12.3 km to the NW`, or `no echo in range` when no
  cell in the alert radius met that product's threshold). Shown as
  `radar=unavailable` when neither product could be fetched.
- `raining_now` — 1 if a PrecipRate cell within `NOW_RADIUS_KM` exceeds
  `RAINING_NOW`, else 0.
- `eta` — minutes until rain from the fastest-arriving trigger, or `none` if no
  trigger carries an ETA. Pirate Weather triggers carry one; radar triggers
  carry one when `DIRECTION_FILTER=1` and motion is known, and rain at the
  house counts as `0min`.
- `sources` — which trigger(s) fired this cycle (`radar`, `pirate weather`, both,
  or `none`).
- `latched` — 1 if the alarm is currently latched (an alert is active), else 0.
- `outcome` — what the cycle actually did: `send`, `repeat`, `skip`, `re-armed`,
  `none`, or `send-failed`.
- `note` — `-` normally, or `moving away` when the direction filter dropped a
  radar echo this cycle.

## Tuning

Start with the defaults. If alerts feel late, lower `MIN_DBZ` to `25` for
earlier — but noisier — alerts. If single-pixel radar noise is triggering false
alarms, raise `MIN_CELLS`. During an imaging session where you want a reminder
that rain is still active, set `REPEAT_MIN=10`. Set `DIRECTION_FILTER=1` to
estimate storm motion from consecutive reflectivity frames and stop alerting
on echoes that are moving away from the site; the alert includes an ETA when
motion is known. The filter projects only the nearest qualifying cell. A broad
line moving crosswise can be suppressed until a different cell becomes
nearest, which shortens lead time. Leave the filter off until you have
replayed recorded storms with it on.

To tune without hammering the network or ntfy, record a stretch of frames and
replay them through the detector in dry-run mode (alerts are logged as
`WOULD SEND ...` instead of being sent). Recorded frames are a subset around
your own coordinates and stay local; `frames/` is gitignored.

```
LAT=.. LON=.. .venv/bin/python scripts/record_frames.py frames/ --minutes 60
LAT=.. LON=.. NTFY_URL=x REPLAY_DIR=frames/ DIRECTION_FILTER=1 .venv/bin/python -m astrorainprotect
```

## Gotchas

- ntfy: the topic is the last path segment of `NTFY_URL`; the token is a
  separate `tk_...` credential. Do not conflate them.
- The ntfy iOS app has had a bug where notifications arrive silent; not something
  this project can fix. `Priority` and emoji tags do not control sound.
- Portainer's web-editor stacks cannot `build:`; hence the GHCR image.
- Docker creates a *directory* if a bind-mounted file path is missing on the
  host; avoid host-file mounts entirely in the new version.
- Pirate Weather free tier has a monthly call cap; polling every 5 minutes fits.
  Do not poll it faster.
- MRMS timestamps are UTC; log in local time but keep the frame timestamp UTC.
