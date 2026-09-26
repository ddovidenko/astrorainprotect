# Changelog

All notable changes to this project are documented here. Format follows
Keep a Changelog; versions follow SemVer.

## [Unreleased]

### Added
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
- `DEBUG=1`/`2` no longer switches on eckit's `PRE-MAIN-DEBUG` startup chatter in the container
  log; the entry point hides the variable while the GRIB libraries load (#13).
