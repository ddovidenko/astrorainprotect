# Tuning against recorded storms

The detector can be replayed over radar frames captured during a real storm, so
you can compare thresholds and the direction filter without waiting for weather
or sending notifications. This page is being written from the first recorded
event; the commands below already work.

## Record

The running alarm records every event by itself (`AUTO_RECORD=1`, the default):
from the first alert, starting with the cached 30 minutes of history, until 30
minutes after re-arm, into `STATE_DIR/recordings/<UTC date-time>/`. The newest
10 recordings are kept. Copy one off the Docker host for replay, for example:

```bash
scp -r host:/opt/astrorainprotect/state/recordings/20261002-0310 frames-2026-10-02/
```

To record by hand (a quiet night, or a machine without the alarm), run this on
any machine with the project installed (see CONTRIBUTING.md), when rain is
expected in the next hour or two. Starting early matters: the motion estimate
needs at least 8 minutes of history, ideally 20, before a cell arrives.

```bash
LAT=<lat> LON=<lon> .venv/bin/python scripts/record_frames.py frames/ --minutes 180 --interval 120
```

Each new radar frame (every 2 minutes, both products) is saved as a ~40 KB
`.npz` file holding just the ±0.5° box around your coordinates. Duplicates are
skipped, nothing is sent, and the state directory is untouched. `frames/` and
`frames-<date>/` are gitignored. Recordings are centred on your coordinates and
give your location away to within a few hundred metres, so never commit them;
use a fresh `frames-<date>/` folder per capture.

## Replay

```bash
set -a; . ./.env; set +a
REPLAY_DIR=frames/ DIRECTION_FILTER=1 .venv/bin/python -m astrorainprotect
```

The clock steps from the first frame to the last in `POLL_SEC` increments and
each step runs the real poll cycle with a throwaway latch, so the frame history,
repeats and the direction filter see what they would have seen live. Keep the
production `POLL_SEC` when counting notifications; set `POLL_SEC=120` (the
product cadence) to run every frame when working on the motion estimator.
Pirate Weather is not called and the scope gate is bypassed. Each cycle prints
the usual summary line, alerts appear as `WOULD SEND <title>: <message>` instead
of going to ntfy, and the last line states the cycle count and cadence.

## What to compare

Run the same folder with different settings and note when the first
`WOULD SEND` appears relative to when `raining_now=1` first shows:

- `MIN_DBZ` 25 vs 30: earlier alerts versus alerts for cells that never arrive.
- `MIN_CELLS`, `ALERT_RADIUS_KM`: lead time versus false alarms.
- `DIRECTION_FILTER` (on by default): how often the summary shows
  `note=motion unknown (...)` (plain alerting; the reason is in the brackets),
  and how many repeats a `moving away (repeats held)` note saves on a storm
  that passed, against how many it holds on a storm that did reach you. The
  filter never holds a first alert or the house trigger, so the cost of a
  wrong "moving away" is one missing repeat.

Worked examples from real captures will be added here.
