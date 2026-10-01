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

## Quick start

You need Docker (or Portainer) and an ntfy topic: install the ntfy app on your
phone (iOS or Android), subscribe to a topic name of your choosing, and use
`https://ntfy.sh/<topic>` as `NTFY_URL`. For a protected topic on your own ntfy
server, also set `NTFY_TOKEN`.

**Portainer**

1. On the Docker host, create the state directory and hand it to the container's
   user: `mkdir -p /opt/astrorainprotect/state && chown 1000:1000
   /opt/astrorainprotect/state`.
2. Add a Git-repository stack pointing at this repo with compose path
   `portainer-stack.yml`.
3. In the stack's Environment variables section, set `LAT`, `LON`, `NTFY_URL`,
   and optionally `NTFY_TOKEN`. Everything else has a working default (see
   Configuration). Set `DEBUG=2` for the first deploy so you get a test
   notification proving the ntfy path works, then drop it to `1` or `0`. (The
   stack file passes it to the container as `ASTRORAINPROTECT_DEBUG`.)
4. Deploy.

**Plain Docker Compose**

```bash
git clone https://github.com/ddovidenko/astrorainprotect
cd astrorainprotect
cp .env.example .env      # fill in LAT, LON, NTFY_URL (and NTFY_TOKEN if needed)
mkdir state               # the container runs as uid 1000; chown 1000:1000 if you are not
docker compose up -d      # builds the image locally; docker compose logs -f to watch
```

The published image is `ghcr.io/ddovidenko/astrorainprotect:latest`; use it in
place of `build: .` if you prefer not to build.

## Configuration

Legacy names preserved; new vars marked.

| Var | Default | Meaning |
|---|---|---|
| `LAT`, `LON` | required | house coordinates |
| `NTFY_URL` | required | server + topic |
| `NTFY_TOKEN` | empty | bearer token for protected topics |
| `NTFY_PRIORITY` (new) | `high` | ntfy priority header |
| `ASTRORAINPROTECT_DEBUG` | 0 | 0/1/2; the stack file maps the Portainer variable `DEBUG` to it |
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
| `SCOPE_HOSTS` | empty | scope-online gate; 5 s connect timeout, one retry, offline only after two missed polls |
| `SNAPSHOT` (new) | 1 | attach a radar snapshot PNG to alerts and the test notification; 0 disables |
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
- **Repeat**: with `REPEAT_MIN > 0`, a "still raining" repeat alert is sent on
  the poll nearest to that many minutes after the last alert (within half a
  `POLL_SEC` early counts, so a clock a few seconds short does not push the
  repeat a whole poll later); with the default `REPEAT_MIN=0`, there is no
  repeat, ever.
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
- **Pirate Weather outage**: with `PW_KEY` set, three consecutive failed polls
  (15 minutes at `POLL_SEC=300`) send one `default`-priority "Pirate Weather
  unavailable" notification, and the first successful poll after it sends
  "Pirate Weather back". Radar alerts are unaffected; the summary line shows
  `pirate=down:N` meanwhile. The fetch gives up after 10 s.
- **Scope announcements**: with `SCOPE_HOSTS` set, any host coming online or
  going offline between polls sends a `default`-priority notification ("Scope
  online: 192.168.1.235 came online. Online: 192.168.1.235. Radar checks active." / "Scope offline: ...
  No scope online; radar checks paused until one returns."), so you know the
  monitor saw the scope you just set up. The last known set is kept in the
  state directory, so a container restart does not re-announce.
- **Startup failures notify**: a config error or an unwritable state directory
  sends one high-priority "astrorainprotect failed to start" message per
  container lifetime before exiting, so a broken deploy is not silent.
- **Rearm**: once no trigger remains (nothing nearby, nothing overhead, no
  Pirate Weather ETA) the latch clears, so the next qualifying cell can alert
  again.
- **Scope gate**: if `SCOPE_HOSTS` is set, the cycle first checks whether any of
  those hosts are online (for example a smart telescope's JSON-RPC port). If none are, the latch is
  cleared and the cycle skips radar/Pirate Weather checks entirely for that
  poll — nothing outside is at risk to protect.
- **DEBUG levels** (`ASTRORAINPROTECT_DEBUG`; a bare `DEBUG` in the
  container is ignored with a startup warning because the GRIB library treats
  that name as its own switch): `0` is silent apart from normal INFO logging.
  `1` adds extra detail lines (which scope hosts are online, the Pirate
  Weather summary). `2` additionally sends a one-time test notification
  on every container start, so you can confirm the ntfy path works without
  waiting for rain. The test is sent before the scope gate and says what the
  gate will do: "Scopes online: ...", "No scope online (...); waiting for one
  before checking radar." or "Scope gate disabled; checking every poll."
- **Direction filter**: with `DIRECTION_FILTER=1`, storm motion is estimated
  by cross-correlating the echo (>= 20 dBZ) in the newest reflectivity frame
  with the frame about 20 minutes earlier, and every qualifying echo is
  projected along it. If its projected path does not come within
  `max(NOW_RADIUS_KM, ALERT_RADIUS_KM / 4)` of the site inside `LOOKAHEAD_MIN`,
  the radar trigger is dropped for that cycle (the summary line shows
  `note=... moving away`, naming the products that were dropped:
  `reflectivity`, `preciprate` or `reflectivity+preciprate`). When it will hit, the alert carries an ETA, counted
  down by the age of the newest frame. When motion is unknown, the filter never
  suppresses: plain radius alerting applies, and the summary line says why:
  `note=motion unknown (no baseline)` (less than 8 minutes of gap-free
  history, or a gap over 30), `(no current reflectivity)` (reflectivity was
  not fetched this poll or is over 10 minutes old), `(radar gaps)` (over 10%
  of the newest frame is missing), `(few cells)` (too little echo in the
  box), `(corr 0.21)` (the two frames do not match at any shift up to
  120 km/h), `(shift at search limit)` (the best match is as far as the
  search reaches, so it is unrelated echo), `(small shift)` (under 2 grid
  cells) or `(no clear shift)` (the frames match about as well without
  moving, as a decaying echo does). Rain at the site always alerts.

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
- `sources` — which trigger(s) fired this cycle: `radar`, `house` (raining at
  the site), `pirate weather`, any combination, or `none`.
- `pirate` — `ok`, `down:N` (consecutive failed polls), `off` (no `PW_KEY`) or
  `skipped` (scope gate closed).
- `latched` — 1 if the alarm is currently latched (an alert is active), else 0.
- `outcome` — what the cycle actually did: `send`, `repeat`, `skip`, `re-armed`,
  `none`, `send-failed`, or `scope-offline` (no scope answered, so nothing was
  checked; the line then shows `radar=skipped`).
- `note` — `-` normally, `reflectivity moving away`, `preciprate moving away`
  or `reflectivity+preciprate moving away` when the direction filter dropped
  the radar echo this cycle, `motion unknown (...)` when it could not
  estimate motion and alerted on plain radius, or `no scope online (...)` on the
  scope-offline path.

## Tuning

Start with the defaults. If alerts feel late, lower `MIN_DBZ` to `25` for
earlier — but noisier — alerts. If single-pixel radar noise is triggering false
alarms, raise `MIN_CELLS`. During an imaging session where you want a reminder
that rain is still active, set `REPEAT_MIN=10`. Set `DIRECTION_FILTER=1` to
estimate storm motion from reflectivity frames and stop alerting
on echoes that are moving away from the site; the alert includes an ETA when
motion is known. Every qualifying cell is projected, so a broad line moving
crosswise alerts as soon as any part of it is headed for the site, with the
ETA of the earliest part. Leave the filter off: on the first recorded
storm it dropped the alert for 40 minutes of a real approach, because new cells
kept forming on the side facing the site (#48).

To tune without waiting for the next storm, record a stretch of radar frames
during one and replay them through the detector as often as you like, with
different settings, in dry-run mode (alerts are logged as `WOULD SEND ...`
instead of being sent). Recorded frames are a small box around your own
coordinates and stay on your machine. The workflow, with worked examples, is
in [docs/tuning.md](docs/tuning.md).

## Gotchas

- ntfy: the topic is the last path segment of `NTFY_URL`; the token is a
  separate `tk_...` credential. Do not conflate them.
- The ntfy iOS app has had a bug where notifications arrive silent; not something
  this project can fix. `Priority` and emoji tags do not control sound.
- Docker creates a *directory* if a bind-mounted path is missing on the host,
  owned by root. Create the state directory yourself and give it to uid 1000.
  The container checks this at startup and exits (code 3) if it cannot write
  there, so a wrong mount shows up as a restart loop in `docker ps` rather than
  as alerts that repeat every poll.
- Pirate Weather free tier has a monthly call cap; polling every 5 minutes fits.
  Do not poll it faster.
- Radar frame times in the log are UTC (`frame=20:12:39Z`); everything else is
  in `TZ`.

## Contributing and design notes

See [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup, tests,
release flow, and where the design spec lives. Bugs and ideas go in
[GitHub issues](https://github.com/ddovidenko/astrorainprotect/issues).
