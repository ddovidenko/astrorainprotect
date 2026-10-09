# Changelog

All notable changes to this project are documented here. Format follows
Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Fixed
- The healthcheck goes unhealthy when nothing external has answered for six polls (radar,
  ntfy, Pirate Weather, scopes, or a `HEAD` on the MRMS bucket on gate-closed cycles). A
  container that had lost its network ran 13 hours showing healthy (#62).
- A repeat fires on the poll nearest to `REPEAT_MIN`, counting up to half a poll early as due.
  With `REPEAT_MIN=10` and `POLL_SEC=300` the live container repeated every third poll (about
  14.5 minutes) because its polls landed a few seconds short of 300 s (#53).
- Storm motion is estimated on real storms. Phase correlation never reached its confidence
  threshold on the first recorded storm (motion unknown on 309 of 309 polls); it is replaced by
  normalised cross-correlation of the >= 20 dBZ echo masks of two reflectivity frames about
  20 minutes apart, chosen by time so the result does not depend on `POLL_SEC`. Sparse masks
  (under 30 cells) and shifts over 120 km/h are rejected, and echo crossing the box edge does
  not wrap to the other side. Motion counts as unknown, so plain radius alerting applies, when
  reflectivity was not fetched this poll or is over 10 minutes old, when either frame has
  coverage gaps, when the best match sits at the edge of the search, and when the frames match
  about as well without moving (a decaying echo). Motion is now estimated whichever product
  qualified, the ETA counts from the frame the echo was found in, and the frame cache holds 16
  frames per product. Replaying the same recording with the site's settings, motion is unknown
  on 44 polls, 37 of them a decaying echo with no clear shift (#47).

### Changed
- The image carries OCI source/description/licence labels, so the GHCR package page links to
  this repo and shows the README; the README gains a prebuilt-image quick start.
- `DIRECTION_FILTER=1` no longer drops the radar trigger for echoes moving away: the first
  alert always goes out, headed "Rain nearby (moving away)" with the closest approach in the
  detail, and only repeats are held while every active trigger recedes. The latch stays set, so
  a storm that turns back produces a repeat, not a fresh alert. Rain at the house and Pirate
  Weather are never held. With that the filter is on by default (`.env.example`, stack file);
  with the default `REPEAT_MIN=0` it changes no decision, only the message and the summary
  line's `eta=` and `note=` fields. On the
  recorded storms it cut 31 notifications to 13 and 7 to 2 with no first alert lost (#48).
- The radar detail in an alert names both products every time, qualifying ones first, the other
  with its state: `reflectivity 28 dBZ, 1 cell, below threshold`, `reflectivity 18 dBZ, under
  threshold`, `reflectivity none in range` or `reflectivity no data`. Products are separated by
  `;`. Consecutive messages for one storm now read alike (#46).
- Replay (`REPLAY_DIR`) steps the clock by `POLL_SEC` instead of running every frame time, so
  notification counts match what the live loop would have sent; `POLL_SEC=120` runs every
  frame (#50).
- With `DIRECTION_FILTER=1` the summary line says why motion is unknown
  (`note=motion unknown (<reason>)`; the README lists the reasons), and the "moving away"
  note names every product concerned (#47).
- Project brief and design spec describe the site generically; CONTRIBUTING asks the same of
  future docs, commits, issues and pull requests.
- CONTRIBUTING.md gains sections on private data that must never be committed and on commit
  and pull request practice; the tuning guide states why recordings stay local.
- CLAUDE.md brought in line with the code: configuration table, repository layout, scope
  probe and debounce, snapshot attachment, all-cell projection, workflow rules (#41).
- Scope check tolerates a scope that is slow to answer while imaging: connect timeout 2 s -> 5 s
  with one retry, and a host counts as offline (notification, gate, latch reset) only after two
  consecutive missed polls. "Came online" stays immediate. `ASTRORAINPROTECT_DEBUG=1` logs each
  host's connect time.
- The debug level is read from `ASTRORAINPROTECT_DEBUG`; a bare `DEBUG` is ignored with a startup
  warning because eckit treats that name as its own switch. The stack file maps the `DEBUG`
  Portainer variable to the new name, so existing stack variables carry over unchanged (#25).

### Added
- Every notification body starts with `[HH:MM]` in `TZ`: the radar frame's time for alerts
  and repeats, the send time for the test notification and announcements. The ntfy app shows
  only the date after a few hours (#64).
- Every new alert starts a recording of the frames behind it under `STATE_DIR/recordings/`:
  the cached history first, then every frame while latched and for 30 minutes after re-arm.
  Newest 10 kept; a folder replays as it is with `REPLAY_DIR`. `AUTO_RECORD=0` disables (#57).
- Pirate Weather outages are visible: the summary line carries `pirate=ok|down:N|off|skipped`,
  and after three consecutive failed polls one default-priority "Pirate Weather unavailable"
  notification goes out, with "Pirate Weather back" on recovery. The fetch timeout drops from
  20 s to 10 s so a slow secondary source holds up a radar alert less (#51).
- Alerts, repeats and the DEBUG=2 test notification carry a radar snapshot PNG as an ntfy
  attachment: the box around the house, house crosshair, alert and hit-radius rings, motion
  vector when known. Rendered with numpy alone; `SNAPSHOT=0` disables; any image failure falls
  back to the text-only send (#16).
- `DIRECTION_FILTER=1` projects every qualifying cell, not only the nearest: a crosswise line
  alerts as soon as any part of it is headed for the site, ETA is the earliest entry (#11).
- Scope online/offline announcements: any `SCOPE_HOSTS` host coming or going between polls
  sends a default-priority notification; state persists across restarts (#22).
- One high-priority "failed to start" notification per container lifetime on config errors or an
  unwritable STATE_DIR (#26).
- Radar-based rain detection from NOAA MRMS (reflectivity + PrecipRate) with the
  legacy alert semantics: one alert per event, optional repeat, scope-online gate,
  DEBUG levels, test notification.
- Pirate Weather kept as a secondary trigger (PW_KEY), polled at most every 5 minutes.
- Docker image (python:3.12-slim, non-root, heartbeat HEALTHCHECK), measured size: 506 MB.
- GitHub Actions CI: ruff + pytest on PRs, image push to ghcr.io/ddovidenko/astrorainprotect on main.
- Portainer Git stack file.
- DIRECTION_FILTER=1: storm motion from consecutive reflectivity frames; echoes moving away do not alert, ETA reported when known.
- scripts/record_frames.py and REPLAY_DIR for tuning against recorded frames.

### Changed
- Container image shrinks from 506 MB to 324 MB by pinning `eccodes==2.39.2` on x86_64 CPython <= 3.13,
  the last wheel whose bundled libeccodes does not link eckit; Python 3.14 and aarch64 keep the
  newer binding because no eckit-free wheel exists for them (#12).
- Startup fails fast with a clear message when STATE_DIR is not writable, instead of
  discovering it on the first alert (#14).
- LAT/LON outside MRMS CONUS coverage (20–55 N, 130–60 W) are rejected at startup (#15).
- S3 listings follow continuation tokens, so a truncated page can never hide the newest
  radar file (#21).
- Radar failures are counted per class (listing, download, decode) in the ERROR lines, and the
  scope-offline poll logs the same field set as every other poll (`outcome=scope-offline`) (#20).
- Wording: a projected ETA under half a minute reads "Rain arriving now"; the direction-filter
  note reads `reflectivity moving away` (#19).
- Rain detected at the house now raises an alert instead of silently re-arming (differs from legacy).
  The notification is titled "Currently raining" with the rate at the house in the body.
- The DEBUG=2 test notification is sent before the scope gate on every start and reports
  whether scopes are online, so a restarted container always announces itself (legacy stayed silent
  until a scope came up).

### Fixed
- "Rain arriving now" no longer carries a stray `eta 0 min` in the radar detail (#32).
- `scripts/record_frames.py` flushes each progress line so a redirected log can be tailed live (#30).
- Docker `HEALTHCHECK` start-period raised from 30 s to 120 s so a slow first cycle does not
  mark a fresh container unhealthy (#31).
- `DEBUG=1`/`2` no longer switches on eckit's `PRE-MAIN-DEBUG` startup chatter in the container
  log; the entry point hides the variable while the GRIB libraries load (#13).
