# Tuning against recorded storms

The detector can be replayed over radar frames captured during a real storm, so
you can compare thresholds and the direction filter without waiting for weather
or sending notifications. This page is being written from the first recorded
event; the commands below already work.

## Record

Run this on any machine with the project installed (see CONTRIBUTING.md), when
rain is expected in the next hour or two. Starting early matters: the motion
estimate needs a few frames of history before a cell arrives.

```bash
LAT=<lat> LON=<lon> .venv/bin/python scripts/record_frames.py frames/ --minutes 180 --interval 120
```

Each new radar frame (every 2 minutes, both products) is saved as a ~40 KB
`.npz` file holding just the ±0.5° box around your coordinates. Duplicates are
skipped, nothing is sent, and the state directory is untouched. `frames/` is
gitignored; recordings are specific to your location.

## Replay

```bash
set -a; . ./.env; set +a
REPLAY_DIR=frames/ DIRECTION_FILTER=1 .venv/bin/python -m astrorainprotect
```

Every distinct frame time is run through the real poll cycle with a throwaway
latch. Pirate Weather is not called and the scope gate is bypassed. Each cycle
prints the usual summary line, and alerts appear as `WOULD SEND <title>:
<message>` instead of going to ntfy.

## What to compare

Run the same folder with different settings and note when the first
`WOULD SEND` appears relative to when `raining_now=1` first shows:

- `MIN_DBZ` 25 vs 30: earlier alerts versus alerts for cells that never arrive.
- `MIN_CELLS`, `ALERT_RADIUS_KM`: lead time versus false alarms.
- `DIRECTION_FILTER=1`: how often the summary shows no ETA (motion unknown,
  plain alerting used) and whether any cycle shows `note=reflectivity moving away` for a
  storm that did reach you. That last case is the one to watch for; if it
  happens on your data, keep the filter off.

Worked examples from real captures will be added here.
