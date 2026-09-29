# astrorainprotect

Self-hosted rain alarm for a home in Cypress (NW Houston), Texas. Purpose: warn me,
via push notification to an iPhone, when rain is heading toward my coordinates, with
enough lead time to bring in astrophotography gear (ZWO Seestar smart telescopes)
left outside overnight. False alarms are cheap; a missed alarm is expensive.

This file is the project brief for Claude Code. Read it fully before touching code.

## Why this project exists (context that drives design)

A first version already runs in production (see `legacy/`). It polls the
**Pirate Weather** API (free, open source, Dark Sky-compatible) every 5 minutes and
posts to **ntfy** when its minute-by-minute forecast shows rain within N minutes.
The plumbing works well; the data source does not. Pirate Weather's "minutely"
data is derived from the NOAA HRRR model, not from radar. On 2026-09-23 it reported
`now=0.0 mm/h, max_prob=0%` for the full hour while it was actually raining at the
house and Windy's radar showed the cell clearly. Gulf Coast pop-up convection forms
in place in under an hour; a model run from earlier in the day never sees it.

Conclusion: the alert trigger must come from **actual weather radar**. Pirate
Weather stays as a secondary signal for organized systems (fronts), where it can
give 30–60 minutes of warning versus ~15–20 from radar extrapolation.

## Goals

1. Detect rain on radar within a configurable radius of `LAT,LON` and push a
   notification to ntfy, with the same behaviour I already rely on (one alert per
   event, optional repeat, scope-online gate, DEBUG levels, test notification).
2. Optionally estimate storm motion from consecutive radar frames so rain that is
   nearby but moving away does not alert (or alerts at lower priority).
3. Keep the Pirate Weather check as a second trigger, unified behind one notifier.
4. Run as a single container deployed from this GitHub repo via a Portainer
   Git-backed stack. No host files other than a state directory.
5. Everything configurable through environment variables; sensible defaults.

## Non-goals

- No Home Assistant integration (I don't use HA).
- No web UI. Logs + push notifications are the interface.
- No paid APIs. Free tiers or public data only.
- Do not build a general weather app. This is an alarm.

## Data source: NOAA MRMS on AWS Open Data

Use the MRMS (Multi-Radar Multi-Sensor) mosaics published on the public S3 bucket
`noaa-mrms-pds` (no credentials needed; use anonymous/unsigned requests). Products
are GRIB2, gzip-compressed, updated every 2 minutes, ~1 km grid (0.01°) covering
CONUS (lat ~20–55 N, lon ~130–60 W).

Primary product: **`PrecipRate`** (surface precipitation rate, mm/h).
Path pattern (verify against the bucket listing before hard-coding; it has changed
before):

```
s3://noaa-mrms-pds/CONUS/PrecipRate_00.00/YYYYMMDD/MRMS_PrecipRate_00.00_YYYYMMDD-HHMMSS.grib2.gz
```

Secondary/alternative product to evaluate: `MergedReflectivityQCComposite_00.50`
(composite reflectivity, dBZ). Reflectivity shows developing cells a few minutes
earlier than PrecipRate registers rain; ~30 dBZ ≈ rain that matters. Consider
using reflectivity for the "incoming" detection and PrecipRate for "raining now".

Notes for the implementation:
- Negative values in MRMS grids mean missing / no coverage. Treat as 0 and log
  if the box around the house is mostly missing (radar outage).
- Find the newest file by listing the day's prefix (and the previous day near
  midnight UTC). Do not assume a fixed filename time.
- Only download the file once per timestamp; cache the last few frames (in memory
  or `/state`) for motion estimation. Full-CONUS files are a few MB each; that
  is fine every 2–5 minutes.
- Subset the grid to a box around `LAT,LON` (e.g. ±0.5°) immediately after
  decoding; never keep whole-CONUS arrays around between polls.
- GRIB decoding: the `eccodes` PyPI package (bundled `eccodeslib` wheel), used
  directly. cfgrib/xarray were evaluated and rejected: MRMS parameters decode as
  `shortName=unknown` and they add 17 MB. Decided 2026-09-23, see
  docs/superpowers/plans/2026-09-23-astrorainprotect.md.
- Poll interval: 2 minutes matches the product cadence; default to 3–5 minutes
  and make it configurable.

## Detection logic

Given the subset grid and its lat/lon coordinates:

1. Compute great-circle distance from every cell to `LAT,LON` (or a fast
   equirectangular approximation, adequate at this scale).
2. **Raining now**: rate at the nearest cell (or max within `NOW_RADIUS_KM`,
   default 1) ≥ `RAINING_NOW` mm/h.
3. **Rain nearby**: any cell within `ALERT_RADIUS_KM` (default 20) with rate ≥
   `MIN_INTENSITY` mm/h (default 0.2) — or reflectivity ≥ `MIN_DBZ` if using
   the reflectivity product. Require a minimum number of qualifying cells
   (`MIN_CELLS`, default 3) so a single noisy pixel doesn't trigger.
4. **Approach filter (optional, `DIRECTION_FILTER=1`)**: compare the qualifying
   echoes in the current frame with the same echoes 2–3 frames earlier (e.g.
   via centroid displacement, or a coarse cross-correlation of the box). Derive
   a motion vector; project every qualifying cell forward (#11); alert only if
   any cell's path enters the hit radius (`max(NOW_RADIUS_KM,
   ALERT_RADIUS_KM / 4)`) in the next `LOOKAHEAD_MIN` minutes, with the ETA of
   the earliest one. Motion comes from whole-box phase correlation of
   reflectivity frames; shifts under 2 cells count as unknown motion. If
   motion cannot be estimated (new cell, too few frames), fall back to plain
   radius alerting — never suppress an alert because the filter lacks data.
5. Report an ETA (minutes) when motion is known; otherwise report distance and
   bearing ("rain 12 km to the SW").

Also keep the Pirate Weather check (ported from `legacy/rain-check.sh`) as an
independent trigger when `PW_KEY` is set. Either source can raise the alert;
the message says which source(s) fired.

## Notification behaviour (must match what I have today)

Port these semantics from `legacy/rain-check.sh` exactly; they are tuned and
tested:

- One alert per rain event, latched in `/state/alerted`. Re-armed only when no
  trigger remains active. Rain detected at the house is itself a trigger
  (notification "Currently raining"); the legacy rule of re-arming while it
  rains was dropped because it silenced cells that form in place over the house.
- `REPEAT_MIN` (default 0): while a trigger stays active, re-send every N
  minutes with updated ETA/distance, title "Rain incoming (still)". Uses the
  latch file's mtime as the timer.
- `SCOPE_HOSTS`: comma-separated `host[:port]` (default port 4700, the Seestar
  JSON-RPC port). If set, skip all checks (and API/S3 fetches) unless at least
  one host counts as online. Reset the latch when none is online so each
  observing session starts clean. Log "no scope online".
  - Probe: TCP connect, 5 s timeout, one retry after 1 s. A Seestar imaging
    outdoors on Wi-Fi power save can miss a short connect while the iOS app
    (persistent connection) still works.
  - Debounce: a host counts as offline only after two consecutive missed polls.
    One miss neither notifies, nor closes the gate, nor clears the latch. A host
    that was not online before gets no grace.
  - Any host coming or going sends a default-priority notification (#22); the
    last known set persists in the state dir so restarts do not re-announce.
- Debug level `0|1|2` (env `ASTRORAINPROTECT_DEBUG`): 1 adds detail lines,
  including each scope's connect time; 2 additionally sends one test
  notification per container start (marker in `/tmp`, cleared on start) through
  the *same* notify code path as real alerts. The test send runs before the
  scope gate and reports the gate state.
- Radar snapshot (#16): alerts, repeats and the test notification attach a
  small PNG of the box (reflectivity, house crosshair, alert and hit-radius
  rings, motion vector when known), rendered with numpy only. Sent as an ntfy
  PUT with the text in headers; any failure falls back to the plain text POST,
  so an image problem never costs an alert. Stale frames are never attached.
  `SNAPSHOT=0` disables.
- Fatal startup errors (bad config, unwritable state dir) send one
  high-priority "failed to start" notification per container lifetime (#26).
- ntfy: POST plain text to `NTFY_URL` (server + topic), headers `Title`,
  `Priority: high`, `Tags: loud_sound,bell`, and `Authorization: Bearer
  $NTFY_TOKEN` only when the token is non-empty. Log ntfy's HTTP status and
  body on failure. Consider `Priority: urgent` for the scope-online case.
- Every poll logs one timestamped summary line so `docker logs` shows it is
  alive: time of the radar frame used, frame age, max rate in radius, nearest
  echo distance/bearing, ETA if known, latch state.
- Log clearly separated errors: S3 listing/download failed, GRIB decode failed,
  Pirate Weather fetch failed, ntfy send failed.

## Configuration (environment variables)

Keep the legacy names where they still apply so my existing Portainer stack
variables carry over.

| Var | Default | Meaning |
|---|---|---|
| `LAT`, `LON` | required | house coordinates |
| `NTFY_URL` | required | e.g. `https://ntfy.example.net/rain` |
| `NTFY_TOKEN` | empty | `tk_...` for protected topics |
| `ASTRORAINPROTECT_DEBUG` | 0 | 0/1/2 as above; the stack file maps the Portainer variable `DEBUG` to it |
| `NTFY_PRIORITY` | high | ntfy priority for rain alerts |
| `POLL_SEC` | 180 | radar poll interval |
| `ALERT_RADIUS_KM` | 20 | alert if qualifying echoes inside this |
| `NOW_RADIUS_KM` | 1 | radius for "raining at the house" |
| `MIN_INTENSITY` | 0.2 | mm/h for a PrecipRate cell to count |
| `MIN_DBZ` | 30 | dBZ for a reflectivity cell to count |
| `MIN_CELLS` | 3 | qualifying cells needed |
| `RAINING_NOW` | 0.05 | mm/h at the house = already raining |
| `DIRECTION_FILTER` | 0 | 1 = ignore echoes moving away |
| `LOOKAHEAD_MIN` | 60 | horizon for approach projection and Pirate Weather |
| `REPEAT_MIN` | 0 | repeat interval while active |
| `SCOPE_HOSTS` | empty | scope-online gate |
| `SNAPSHOT` | 1 | attach a radar snapshot PNG to alerts |
| `PW_KEY` | empty | enables Pirate Weather secondary trigger |
| `MIN_PROB` | 0.3 | Pirate Weather probability threshold |
| `REPLAY_DIR` | empty | run the detector over saved frames and exit (local tuning only) |
| `STATE_DIR` | /state | latch/heartbeat directory; fixed by the image in production |
| `TZ` | America/Chicago | for log timestamps |

## Repository layout

```
.
├── CLAUDE.md                 this file
├── README.md                 short user-facing setup/tuning guide
├── Dockerfile                python:3.12-slim + grib deps, non-root, HEALTHCHECK
├── docker-compose.yml        for local dev (build: .)
├── portainer-stack.yml       image: ghcr.io/ddovidenko/astrorainprotect:latest, no build
├── astrorainprotect/
│   ├── __main__.py           entry point
│   ├── app.py                poll loop, cycle wiring, scope debounce, startup
│   ├── alarm.py              decide(): latch/repeat/re-arm and message text (pure)
│   ├── config.py             env parsing + validation, printed at startup
│   ├── mrms.py               S3 listing/download/decode/subset, frame cache
│   ├── frame.py              Frame dataclass, .npz save/load
│   ├── detect.py             radius detection (pure functions, no I/O)
│   ├── motion.py             phase-correlation motion + all-cell projection (pure)
│   ├── snapshot.py           radar PNG renderer, numpy + zlib (pure)
│   ├── scope.py              Seestar reachability probe
│   ├── pirate.py             Pirate Weather secondary trigger
│   ├── notify.py             ntfy client, attachment PUT with text fallback
│   ├── state.py              latch/repeat/test-marker/scope-set/heartbeat files
│   ├── replay.py             REPLAY_DIR mode, dry-run notifier
│   └── healthcheck.py        Docker HEALTHCHECK: heartbeat staleness
├── tests/                    one test_<module>.py per module, plus test_smoke.py
│   └── fixtures/             S3 listing and Pirate Weather samples
├── docs/tuning.md            record -> replay -> compare workflow
├── scripts/record_frames.py  grab N minutes of MRMS subsets for replay
├── legacy/                   the working shell version, kept for reference
└── .github/workflows/ci.yml  pytest + docker build + push to GHCR on main
```

## Development workflow

- I work in a WSL Ubuntu container with Docker available; Portainer runs on my
  home Docker host and can pull from GitHub and GHCR.
- Start by writing `detect.py` with unit tests against synthetic grids, then
  `mrms.py`, then wire the loop. Record real frames with
  `scripts/record_frames.py` during the next rain and add them as fixtures,
  including a replay mode (`REPLAY_DIR=...`) that runs the detector over saved
  frames so tuning does not require waiting for weather.
- `docker compose up --build` must work locally with a `.env`.
- CI builds a multi-arch (amd64 at minimum) image and pushes
  `ghcr.io/ddovidenko/astrorainprotect:latest` and a git-sha tag.
- `portainer-stack.yml` references the GHCR image and only needs the env vars
  and a `/opt/astrorainprotect/state:/state` volume. Deployed as a Portainer
  Git stack pointing at this repo, with auto-update on push if convenient.
- Commit small; conventional commit messages; keep a CHANGELOG.
- Stage files by path and read `git status --short` before every commit; never
  `git add -A`. Recorded frames (`frames/`, `frames-*/`) are a box centred on
  the house and must never be committed; the repo is public.
- `main` is protected by a ruleset (PR required, no force-push, linear
  history). Open each PR against `main`; do not stack PRs, because GitHub
  closes a stacked PR when its base branch is deleted by a squash merge.
- Design spec: docs/superpowers/specs/2026-09-23-astrorainprotect-design.md
- Never merge a branch or PR without asking the user first.

## Known gotchas (learned the hard way)

- ntfy: the topic is the last path segment of `NTFY_URL`; the token is a
  separate `tk_...` credential. Do not conflate them.
- ntfy iOS app has had a bug where notifications arrive silent; not something
  this project can fix. `Priority` and emoji tags do not control sound.
- Portainer's web-editor stacks cannot `build:`; hence the GHCR image.
- Docker creates a *directory* if a bind-mounted file path is missing on the
  host; avoid host-file mounts entirely in the new version.
- Pirate Weather free tier has a monthly call cap; polling every 5 minutes fits.
  Do not poll it faster.
- MRMS timestamps are UTC; log in local time but keep the frame timestamp UTC.
- eckit (loaded by eccodes) treats a bare `DEBUG` environment variable as its
  own switch and floods stdout. The container must never see `DEBUG`; hence
  `ASTRORAINPROTECT_DEBUG`.
- Grid cells are not square in km (about 0.96 km east-west by 1.11 km
  north-south at this latitude); anything drawn or measured in pixels must
  scale the two axes separately.

## Future ideas (not now)

- Query Seestar imaging state via seestar_alp / seestarpy instead of a TCP
  reachability check (new firmware requires an extracted PEM for auth).
- Per-product motion estimation, or reflectivity-only projection (#40).
- Second topic with quieter daytime thresholds.
