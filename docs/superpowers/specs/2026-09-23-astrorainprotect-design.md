# astrorainprotect — design spec

Date: 2026-09-23
Status: approved 2026-09-23; amended by the implementation plan (see its "Spec amendments" section)

## 1. Purpose

Self-hosted rain alarm for a house in Cypress (NW Houston), Texas. It pushes a
notification to an iPhone via ntfy when rain is heading for the house, early
enough to bring in ZWO Seestar telescopes left outside overnight. A missed alarm
is expensive; a false alarm is cheap.

The currently deployed version (`legacy/rain-check.sh`) polls Pirate Weather's
minutely forecast. That forecast is model-derived and misses Gulf Coast pop-up
convection. The new version triggers from actual NOAA MRMS radar and keeps
Pirate Weather as a secondary trigger for organized systems.

Successor to the `rain-radar-alert` brief; everything is renamed
`astrorainprotect` (package, image `ghcr.io/ddovidenko/astrorainprotect`).

## 2. Goals and non-goals

Goals:

1. Detect rain on radar within a configurable radius of `LAT,LON` and push to
   ntfy with the legacy alert semantics (one alert per event, optional repeat,
   scope-online gate, DEBUG levels, test notification).
2. Estimate storm motion from consecutive radar frames so echoes moving away do
   not alert. Never suppress an alert when motion is unknown.
3. Keep Pirate Weather as an independent secondary trigger behind one notifier.
4. Run as one container from GHCR, deployed by a Portainer Git stack, with only a
   `/state` volume.
5. Configure everything through environment variables with sensible defaults,
   keeping the legacy variable names.

Non-goals: Home Assistant, web UI, paid APIs, general weather app features.

## 3. Decisions made during design

| Decision | Choice | Reason |
|---|---|---|
| Name | `astrorainprotect` everywhere | Matches repo and GitHub remote |
| Radar strategy | Reflectivity for "incoming", PrecipRate for "raining now"; either product can satisfy "nearby" | Reflectivity flags developing cells minutes earlier |
| Phasing | One spec, one phased plan | Motion estimation shares the frame cache and message format; design once |
| GRIB decoder | `eccodes` PyPI package used directly (bundled `eccodeslib` wheel); no cfgrib/xarray | Spike 2026-09-23: MRMS params decode as `shortName=unknown` under cfgrib; eccodes alone is 49 MB and decodes CONUS in 0.8 s |
| S3 access | Anonymous HTTPS `?list-type=2` listing and plain GET, no boto3 | Bucket is public; boto3 adds weight for nothing |
| Local Docker | Install Docker Engine in WSL via sudo at the container phase | Not currently installed; needed for `docker compose up --build` |
| Rain at the house | Alerts (ETA 0) instead of re-arming | Final review 2026-09-23: legacy rule silenced in-place convection |

## 4. Architecture

One Python 3.12 process, one poll loop, package `astrorainprotect`.

```
astrorainprotect/
  __main__.py   calls app.main()
  app.py        poll loop, wiring, logging setup
  healthcheck.py Docker HEALTHCHECK entry point (heartbeat age)
  replay.py     REPLAY_DIR mode over recorded frames
  config.py     env → frozen Config dataclass, validation, startup print (token masked)
  mrms.py       S3 listing, newest-key selection, download, gunzip, decode, subset → Frame; frame cache
  detect.py     pure: Frame + thresholds → Detection
  motion.py     pure: recent Frames → Motion (vector, confidence) → Approach (will_hit, eta_min)
  alarm.py      pure: triggers + latch state + config → Action (send | repeat | skip | rearm | none)
  pirate.py     Pirate Weather fetch + threshold logic ported from the jq in legacy
  notify.py     ntfy POST with legacy headers
  state.py      latch file, repeat timer via mtime, test marker, heartbeat
  scope.py      TCP reachability of SCOPE_HOSTS
```

Runtime dependencies: `numpy`, `eccodes`, `httpx`.

### Data types

- `Frame`: product name, valid time (UTC), 1-D lat and lon arrays, 2-D values
  (mm/h or dBZ, clamped to ≥ 0), `missing_fraction`. Missing is per product:
  PrecipRate `< 0` (`-1` missing, `-3` no coverage); reflectivity `< -990`
  (`-999` missing; `-99` means no echo and small negatives are real weak echo).
- `Detection`: `raining_now: bool`, `rate_at_house: float`, `qualifying_cells:
  int`, `nearest_km: float | None`, `nearest_bearing_deg: float | None`,
  `max_value: float`, `product: str`.
- `Motion`: `u_km_per_min`, `v_km_per_min`, `confidence` (0–1), `frames_used`.
- `Approach`: `will_hit: bool`, `eta_min: float | None`.
- `Action`: enum `SEND`, `REPEAT`, `SKIP`, `REARM`, `NONE`, with a message and
  title attached for `SEND`/`REPEAT`.

### Module contracts

`detect.detect(frame, home_lat, home_lon, *, now_radius_km, alert_radius_km,
now_threshold, nearby_threshold, min_cells) -> Detection`

- Distances use an equirectangular approximation; adequate at ±0.5°.
- `raining_now` is true when the max value within `now_radius_km` ≥
  `now_threshold`. The loop only uses this field from the PrecipRate
  detection (with `RAINING_NOW`); for reflectivity it is ignored.
- `qualifying_cells` counts cells within `alert_radius_km` with value ≥
  `nearby_threshold` (`MIN_DBZ` or `MIN_INTENSITY` by product). "Nearby" is true when `qualifying_cells ≥ min_cells`.
- `nearest_km`/`nearest_bearing_deg` describe the closest qualifying cell.

`motion.estimate(frames: Sequence[Frame]) -> Motion | None`

- Requires ≥ 3 frames of the same product with distinct valid times.
- Uses FFT phase correlation between the oldest and newest frame in the window
  to recover integer-cell displacement, converts to km/min using the frame
  time delta and cell size at the home latitude.
- Returns `None` when frames are insufficient, the correlation peak is weak
  (confidence below a fixed floor), or the box is mostly empty.

`motion.project(detection, motion, *, hit_radius_km, lookahead_min) -> Approach`

- Projects the nearest qualifying echo forward along the motion vector.
  `will_hit` is true when the path comes within `hit_radius_km` of the house
  inside `lookahead_min`; `eta_min` is the time it first enters that radius.
  The loop passes `hit_radius_km = max(NOW_RADIUS_KM, ALERT_RADIUS_KM / 4)`.
  The alert radius itself cannot be the test: the nearest qualifying echo is by
  definition already inside it.

`alarm.decide(inputs: AlarmInputs) -> Action`

Ported from `legacy/rain-check.sh`, with one deliberate change (amended in
the final review, 2026-09-23). In the shell script, "already raining" empties
`eta`, which then re-arms the latch; that rule silenced cells forming in place
over the house, so it is not carried over:

- Raining at the house (PrecipRate within `NOW_RADIUS_KM` ≥ `RAINING_NOW`) is a
  radar trigger with ETA 0 (message headline "Rain at the house now (...)"). It
  sends, skips or repeats like any other trigger. `REARM` happens only when no
  trigger remains. Pirate Weather's "raining now" produces no trigger and never
  suppresses one.
- Any active trigger and no latch → `SEND`.
- Any active trigger, latch present, `REPEAT_MIN > 0`, latch age ≥ `REPEAT_MIN`
  minutes → `REPEAT` (title "Rain incoming (still)"; touching the latch resets
  the timer).
- Any active trigger, latch present, otherwise → `SKIP`.
- No active trigger and latch present → `REARM`.
- Otherwise `NONE`.
- With `DIRECTION_FILTER=1`, a radar trigger is active only if
  `Approach.will_hit` is true or motion is `None`. Motion is never a reason to
  suppress when it is unknown.

## 5. Poll cycle

Every `POLL_SEC` seconds:

1. **Scope gate.** If `SCOPE_HOSTS` is set and no host accepts a TCP connect
   (2 s timeout, default port 4700): log "no scope online", remove the latch,
   skip everything else.
2. **Fetch radar.** For each product (`MergedReflectivityQCComposite_00.50`,
   `PrecipRate_00.00`): list today's UTC prefix, and yesterday's when within 10
   minutes of 00:00 UTC; take the lexically greatest key. If its timestamp
   equals the cached newest frame, skip the download. Otherwise GET, gunzip in
   memory, decode, subset to ±0.5° around the house, push onto that product's
   deque (max 5). Full-CONUS arrays are dropped immediately after subsetting.
3. **Radar availability.** Radar is unavailable this cycle if the newest frame
   is older than 15 minutes or `missing_fraction > 0.5`. Log a warning; radar
   triggers are treated as inactive but the latch is not touched.
4. **Detect.** Raining now: PrecipRate within `NOW_RADIUS_KM` ≥ `RAINING_NOW`
   (adds a radar trigger with ETA 0).
   Nearby: reflectivity ≥ `MIN_DBZ` with `MIN_CELLS`, or PrecipRate ≥
   `MIN_INTENSITY` with `MIN_CELLS`. Either satisfies the radar trigger; the
   message names the product(s).
5. **Motion.** When `DIRECTION_FILTER=1`, run `motion.estimate` on the
   reflectivity deque and `motion.project` on the reflectivity detection.
6. **Pirate Weather.** When `PW_KEY` is set and at least 300 s have passed since
   the last call, fetch and compute ETA using the legacy thresholds
   (`LOOKAHEAD_MIN`, `MIN_PROB`, `MIN_INTENSITY`, `RAINING_NOW`). The 300 s
   floor holds regardless of `POLL_SEC`.
7. **Decide and act.** `alarm.decide`, then POST to ntfy and touch/remove the
   latch. Message content: ETA when known, otherwise distance and bearing
   ("rain 12 km to the SW"), plus the sources that fired.
8. **Log** one summary line: local time, frame valid time (UTC) and age, max
   value in radius per product, nearest echo distance/bearing, ETA if known,
   sources active, latch state.
9. Touch the heartbeat file.

DEBUG=2 sends one test notification per container start through the same
`notify` code path, marker in `/tmp` (cleared on start).

## 6. Notifications

POST plain text to `NTFY_URL`. Headers: `Title`, `Priority: $NTFY_PRIORITY`
(default `high`), `Tags: loud_sound,bell`, and `Authorization: Bearer
$NTFY_TOKEN` only when non-empty. On non-200, log the status and up to 300
bytes of body; the latch is only touched on success.

Titles: "Rain incoming", "Rain incoming (still)", "Rain alert test".

## 7. Configuration

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
| `RAINING_NOW` | 0.05 | mm/h at the house = already raining |
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

## 8. Error handling

Each failure class logs its own `ERROR` line and the loop continues:
S3 listing, S3 download, GRIB decode, Pirate Weather fetch, ntfy send. A
consecutive-failure counter per class is included in the log line. Transient
failures never crash the container.

Container `HEALTHCHECK` runs a tiny script that fails if the heartbeat file is
older than `3 × POLL_SEC`.

The container runs as a non-root user (uid 1000). The host state directory must
be writable by that uid; documented in the README.

## 9. Replay and recording

- `scripts/record_frames.py` polls MRMS for N minutes and saves each subset
  `Frame` as `.npz` (values, lat, lon, valid time, product) into a directory.
- `REPLAY_DIR=<dir>` makes `__main__` iterate the saved frames in time order,
  run detection, motion, and alarm logic with an in-memory latch, and print
  every action that would have been taken. No network calls, no ntfy sends.
  Exits when frames run out.

## 10. Testing

Unit tests (pytest, no network):

- `detect`: single pixel below `MIN_CELLS` ignored; exactly `MIN_CELLS`
  qualifies; cell just inside and just outside the radius; bearing in each
  quadrant; negative (missing) values treated as 0; `missing_fraction`.
- `motion`: synthetic blob shifted by a known offset across three frames
  recovers the vector; ETA for a blob moving toward the house; `will_hit`
  false for a blob moving away; returns `None` for two frames or an empty box.
- `alarm`: table covering first send, skip while latched, repeat after
  `REPEAT_MIN`, no repeat when `REPEAT_MIN=0`, send on rain at the house
  (ETA 0), skip it while latched, rearm when no trigger, direction-filter suppression, and no suppression when
  motion is `None`.
- `config`: defaults, required-var failure, range validation, masking.
- `pirate`: ETA and summary computed from a captured Pirate Weather JSON
  fixture, matching the jq results.
- `mrms`: newest-key selection from fake listing XML, including the midnight
  previous-day case; subset window bounds.
- `scope`: parsing of `host`, `host:port`, and comma lists.

Integration test, marked and skipped when offline: download the newest
PrecipRate file from S3, decode, subset, and assert the grid shape and value
range. Full-CONUS files are not vendored.

## 11. Repository layout

```
.
├── CLAUDE.md                 project brief, adapted to the new name
├── README.md                 setup and tuning guide
├── CHANGELOG.md
├── Dockerfile                python:3.12-slim + grib deps, non-root, HEALTHCHECK
├── docker-compose.yml        local dev, build: .
├── portainer-stack.yml       image: ghcr.io/ddovidenko/astrorainprotect:latest
├── pyproject.toml
├── astrorainprotect/         package as in section 4
├── tests/
│   ├── fixtures/             .npz subset frames, Pirate Weather JSON, listing XML
│   └── test_*.py
├── scripts/record_frames.py
├── legacy/                   the deployed shell version, copied for reference
├── docs/superpowers/specs/   this spec
└── .github/workflows/ci.yml
```

## 12. CI and delivery

- GitHub Actions: on pull requests run pytest and `docker build` (no push). On
  `main` push `ghcr.io/ddovidenko/astrorainprotect:latest` and `:<git-sha>`.
  amd64 only.
- Work happens on feature branches with conventional commit messages. Pull
  requests are opened for review; **merging is always the user's decision**.
- `portainer-stack.yml` references the GHCR image, the env vars above, and a
  `/opt/astrorainprotect/state:/state` volume.

## 13. Implementation phases

1. Project skeleton, `config.py`, `detect.py` with tests, and the GRIB decoder
   spike (throwaway; result recorded in this spec's decision table and the
   Dockerfile).
2. `mrms.py` with listing/decode/subset and the integration test.
3. `alarm.py`, `state.py`, `scope.py`, `notify.py`, `__main__.py` loop; the
   shell semantics fully ported and tested.
4. `pirate.py` secondary trigger.
5. Dockerfile, compose, CI, Portainer stack, README, CHANGELOG. Deployable
   from here; can replace the legacy stack.
6. `motion.py`, `DIRECTION_FILTER`, `scripts/record_frames.py`, `REPLAY_DIR`.
