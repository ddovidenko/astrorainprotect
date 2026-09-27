# Changelog

All notable changes to this project are documented here. Format follows
Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Changed
- The debug level is read from `ASTRORAINPROTECT_DEBUG`; a bare `DEBUG` is ignored with a startup
  warning because eckit treats that name as its own switch. The stack file maps the `DEBUG`
  Portainer variable to the new name, so existing stack variables carry over unchanged (#25).

### Added
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
