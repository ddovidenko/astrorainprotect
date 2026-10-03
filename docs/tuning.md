# Tuning against recorded storms

The detector can be replayed over radar frames captured during a real storm, so
you can compare thresholds and the direction filter without waiting for weather
or sending notifications. The numbers in this guide come from the first three
recorded events at one site in the southern US (autumn 2026); the commands work
for any site.

## Record

The running alarm records every event by itself (`AUTO_RECORD=1`, the default):
from the first alert, starting with the cached 30 minutes of history, until 30
minutes after re-arm, into `STATE_DIR/recordings/<UTC date-time>/`. The newest
10 recordings are kept. Copy one off the Docker host for replay, for example:

```bash
scp -r host:/opt/astrorainprotect/state/recordings/20261002-0310 frames-2026-10-02/
```

To record by hand (a quiet night, or a machine without the alarm), run this on
any machine with the project installed (see CONTRIBUTING.md):

```bash
LAT=<lat> LON=<lon> .venv/bin/python scripts/record_frames.py frames/ --minutes 180 --interval 120
```

Start it the moment the alarm first fires, not when rain is forecast and not
when you notice it is raining. Two of the first three captures began 45 minutes
after the first alert and hold only the recession; the approach, which is the
half you tune on, was gone. The motion estimate also needs 8 minutes of history
(ideally 20) before a cell arrives. Capturing for 24 hours costs about 8 MB.

Each new radar frame (both products) is saved as a ~40 KB `.npz` file holding
just the ±0.5° box around your coordinates. Duplicates are skipped, nothing is
sent, and the state directory is untouched. `frames/` and `frames-<date>/` are
gitignored. Recordings are centred on your coordinates and give your location
away to within a few hundred metres, so never commit them; use a fresh
`frames-<date>/` folder per capture. Save the container's log next to the
frames (`docker logs astrorainprotect > frames-<date>/live.log`) before any
rebuild; it is the only record of what the alarm actually sent.

## Replay

```bash
set -a; . ./.env; set +a
REPLAY_DIR=frames-2026-09-29/ PW_KEY= SCOPE_HOSTS= .venv/bin/python -m astrorainprotect
```

The clock steps from the first frame to the last in `POLL_SEC` increments and
each step runs the real poll cycle with a throwaway latch, so the frame history,
repeats and the direction filter see what they would have seen live. Keep the
production `POLL_SEC` when counting notifications; set `POLL_SEC=120` (the
product cadence) to run every frame when working on the motion estimator.
Pirate Weather is not called and the scope gate is bypassed. Each cycle prints
the usual summary line, alerts appear as `WOULD SEND <title>: <message>` instead
of going to ntfy, and the last line states the cycle count and cadence.

A sweep is a shell loop:

```bash
for cfg in "MIN_DBZ=25" "MIN_DBZ=30" "MIN_CELLS=10" "ALERT_RADIUS_KM=15" "DIRECTION_FILTER=0"; do
  out=$(env $cfg REPLAY_DIR=frames-2026-09-29/ PW_KEY= SCOPE_HOSTS= .venv/bin/python -m astrorainprotect 2>&1)
  echo "$cfg first=$(echo "$out" | grep -m1 outcome=send | sed -E 's/.*frame=([0-9:]{5}).*/\1/')" \
       "sends=$(echo "$out" | grep -c 'WOULD SEND') re-arms=$(echo "$out" | grep -c re-armed)"
done
```

## The recordings behind this guide

| Capture | What happened | Use |
|---|---|---|
| 2026-09-26, 5 h, quiet | forecast storm never reached the box, peak 14 dBZ | false-alarm baseline: 0 alerts at `MIN_DBZ` 25, 15 and 10 |
| 2026-09-28, 5 h, quiet | peak 10 dBZ, PrecipRate 0.0 throughout | same |
| 2026-09-29, 6 h | storm approached from the S, core passed 4 km E then N, receded SW; no rain at the site | lead time, direction filter |
| 2026-09-30 afternoon, 6 h | started 45 min late; recession only, exit to the NW then N | direction filter on a second track |
| 2026-09-30 to 10-01, 24 h | several cells, hours of rain at the site, verified against camera footage | house trigger, repeats |

Settings for every number below unless stated: `ALERT_RADIUS_KM=25`,
`MIN_DBZ=25`, `MIN_INTENSITY=0.1`, `MIN_CELLS=3`, `NOW_RADIUS_KM=1`,
`RAINING_NOW=0.05`, `POLL_SEC=300`, `REPEAT_MIN=10`, `DIRECTION_FILTER=1`.

## What the knobs did

Sweep over the three storm recordings (first alert is the frame time, UTC):

| Setting | 09-29: first, sends | 09-30 pm: first, sends | 24 h: first, sends |
|---|---|---|---|
| defaults above | 17:30, 13 | 19:14, 2 | 03:56, 86 |
| `MIN_DBZ=30` | 17:30, 13 | 19:14, 2 | 03:56, 85 |
| `MIN_INTENSITY=1.0` | 17:30, 13 (3 re-arms) | 19:14, 3 | 03:56, 85 |
| `MIN_CELLS=10` | 17:30, 12 (3 re-arms) | 19:14, 2 | 04:06, 84 |
| `ALERT_RADIUS_KM=15` | **18:00**, 10 (5 re-arms) | 19:14, 2 | **04:06**, 74 |
| `DIRECTION_FILTER=0` | 17:30, 31 | 19:14, 7 | 03:56, 94 |
| `REPEAT_MIN=0` | 17:30, 1 | 19:14, 1 | 03:56, 5 |

What this says:

- **PrecipRate is the sensitive trigger, not reflectivity.** 0.1 or 0.2 mm/h
  corresponds to roughly 15 to 18 dBZ, far below `MIN_DBZ=25`, so on every
  event PrecipRate qualified first and `MIN_DBZ` 25 versus 30 changed nothing.
  Tune `MIN_INTENSITY` if the first alert is too eager; tune `MIN_DBZ` only if
  you want reflectivity to lead, in which case set it near 20.
- **`ALERT_RADIUS_KM` is the lead-time knob.** Cutting it from 25 to 15 km cost
  30 minutes of warning on 09-29 and 10 minutes on the 24-hour event, and the
  alarm re-armed and re-alerted more often as cells crossed the smaller circle.
- **`MIN_CELLS` and `MIN_INTENSITY` trade a few notifications for extra
  re-arms.** Raising either makes the trigger flicker on marginal echo, which
  produces more fresh "Rain incoming" alerts, not fewer notifications. Leave
  them at 3 and 0.1 unless single-pixel noise is a problem at your site.
- **The direction filter removes most repeats on a passing storm.** 31 to 13
  on 09-29, 7 to 2 on 09-30, with the first alert and the re-arm unchanged. On
  a day of rain at the site it saves little (94 to 86), because the house
  trigger is never held, which is the point.
- **`REPEAT_MIN` sets the volume.** 1 notification per event at 0; about one
  per 10 minutes while anything qualifies otherwise. 10 minutes was chosen for
  observing sessions, where a reminder that rain is still active is wanted.

## Lead time

- **09-29:** first alert 17:30Z on PrecipRate at 19.9 km, core closest at
  19:25Z to 19:55Z, 4 km away. About two hours of warning for a storm that
  then missed.
- **09-30 afternoon (live log):** a one-poll alert at 13:10 local on 1 mm/h at
  7.9 km S, re-armed at 13:14; the main alert at 13:29 on a 36 dBZ line 21 km
  SW; rain at the site at 13:33 from a cell that formed in place over the
  house while the line was still 20 km out. Lead time from the first alert:
  about 20 minutes, from the main alert: 4 minutes. In-place convection gives
  no warning beyond the house trigger; that is the case this project exists
  for, and the reason a single-poll alert is not filtered out (#58).
- **24 h recording:** something qualified inside 25 km for the whole day, so
  the alarm stayed latched from the first frame and no lead time can be read
  off it. With `REPEAT_MIN=0` that day would have produced one notification
  at 22:58 local and nothing more, including for the hours of rain at the
  site that started 14 hours later; that is the case for `REPEAT_MIN=10`.

Expect anything from a few minutes to two hours. The radar sees cells that
exist; it cannot see the one that forms over the house.

## The house trigger, checked against a camera

On 2026-10-01 the owner read rain state off motion-triggered security-camera
footage and compared it with `raining_now` and the PrecipRate value of the
1 km cell over the house:

- Every interval of rain seen on camera after 16:30 local had `raining_now=1`
  and every dry spell had it off, with 2 to 5 minutes of latency (frame
  latency plus the 5-minute poll).
- Intensity in the house cell followed the footage: 27 to 46 mm/h during the
  heavy burst, 2 mm/h when it slowed, 3 to 6 mm/h during drizzle phases.
- A 111 mm/h core 2.1 km away did not register at the house, and the camera
  agreed; on 09-29 a 75 mm/h core 4 km away likewise. `NOW_RADIUS_KM=1` is
  right: widening it would report neighbours' rain as yours.
- A few drops of morning drizzle did not register at all. MRMS PrecipRate has
  a floor; it is not a drizzle detector.

So `NOW_RADIUS_KM=1` and `RAINING_NOW=0.05` are confirmed and should not be
tuned on a hunch.

## The direction filter

With `DIRECTION_FILTER=1` (the default) the alarm estimates storm motion from
reflectivity frames about 20 minutes apart and projects every qualifying cell.
It never drops an alert: it adds an ETA when a cell will hit, and holds repeats
while every echo is moving away. Things learned from the recordings:

- Motion was known on about 75 percent of the frames with echo in range.
  The rest show `note=motion unknown (...)`: `no baseline` in the first
  minutes, `few cells` and `no clear shift` in the decaying tail. Plain
  alerting applies then.
- The vector was steady and matched the observed track (median speed 15 to
  17 km/h, heading within 10° of the real one on 134 of 136 frames).
- Suppressing alerts on "moving away", the original design, dropped 40
  minutes of a real approach on 09-29 because new cells formed on the side
  facing the site. Holding repeats instead gives the same quiet with no risk
  to the first alert. Do not reintroduce suppression.
- Whole-cell resolution over 20 minutes is 4 to 5 cells for a slow storm, so
  speed is known to roughly ±10 percent and heading to about ±7°. ETAs are
  honest to about a quarter of their value.

When replaying your own storms, look at `moving away (repeats held)` cycles for
a storm that did reach you: each is one repeat you would not have received.

## Facts about the products

- **Reflectivity** files arrive every 2 minutes with a new grid each time on
  a normal day. **PrecipRate** files also arrive every 2 minutes but repeat
  the previous grid 15 to 40 percent of the time (273 of 708 frames on the
  24-hour recording), so its effective update is often 4 to 6 minutes during
  heavy rain. Reflectivity repeated too on that busy day (152 of 719). This
  bounds the house trigger's latency; nothing in the alarm can shorten it.
- Frame latency (file time to availability) was 1 to 3 minutes throughout.
- Pirate Weather, where it worked at all, fired last: on 10-01 it joined the
  alert at 21:14 local while it had been pouring for an hour. It timed out for
  the entire onset of the 09-29 event. Treat it as a bonus for organised
  systems, not a trigger to tune.

## A note on clocks

Repeats are judged by wall-clock age of the latch file. On a WSL host whose
clock drifted (polls 278 to 299 s apart with `POLL_SEC=300`) the 10-minute
repeat slipped to every third poll until the rule became "within half a poll
of the interval" (#53). If your repeats arrive at an odd cadence, check the
host clock before touching `REPEAT_MIN`.
