"""Poll loop and wiring."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from astrorainprotect.alarm import Action, AlarmInputs, Trigger, decide
from astrorainprotect.config import Config, ConfigError, describe, load_config
from astrorainprotect.detect import Detection, detect
from astrorainprotect.frame import Frame
from astrorainprotect.motion import estimate, project
from astrorainprotect.mrms import MrmsError, RadarSource
from astrorainprotect.notify import Notifier
from astrorainprotect.pirate import PirateError, PirateSource
from astrorainprotect.scope import online_hosts, parse_hosts
from astrorainprotect.state import State

log = logging.getLogger("astrorainprotect")

RADAR_MAX_AGE = timedelta(minutes=15)
RADAR_MAX_MISSING = 0.5
TITLE_TEST = "Rain alert test"


@dataclass(frozen=True)
class RadarStatus:
    available: bool
    reason: str


def radar_status(frame: Frame | None, now: datetime) -> RadarStatus:
    if frame is None:
        return RadarStatus(False, "no frame")
    age = now - frame.valid_time
    if age > RADAR_MAX_AGE:
        return RadarStatus(False, f"stale frame ({age.total_seconds() / 60:.0f} min old)")
    if frame.missing_fraction > RADAR_MAX_MISSING:
        return RadarStatus(False, f"{frame.missing_fraction:.0%} of box missing")
    return RadarStatus(True, "ok")


@dataclass
class App:
    cfg: Config
    radar: Any            # RadarSource or a fake with the same two methods
    notifier: Any         # Notifier or a fake
    state: State
    scope_check: Callable[[], list[str]]
    pirate: Any           # PirateSource (Task 14) or None
    clock: Callable[[], datetime]
    failures: dict[str, int] | None = None
    last_note: str = ""

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = {"radar": 0, "pirate": 0, "ntfy": 0}


def _detect_product(
    app: App, product: str, now: datetime
) -> tuple[Detection | None, RadarStatus]:
    frame = app.radar.fetch_latest(product, now)
    status = radar_status(frame, now)
    if not status.available:
        return None, status
    cfg = app.cfg
    if product == "reflectivity":
        d = detect(frame, cfg.lat, cfg.lon, now_radius_km=cfg.now_radius_km,
                   alert_radius_km=cfg.alert_radius_km, now_threshold=float("inf"),
                   nearby_threshold=cfg.min_dbz, min_cells=cfg.min_cells)
    else:
        d = detect(frame, cfg.lat, cfg.lon, now_radius_km=cfg.now_radius_km,
                   alert_radius_km=cfg.alert_radius_km, now_threshold=cfg.raining_now,
                   nearby_threshold=cfg.min_intensity, min_cells=cfg.min_cells)
    return d, status


def radar_trigger(app: App, dets: list[Detection], now: datetime) -> Trigger | None:
    """Build the radar Trigger from qualifying detections, applying the direction filter."""
    hits = [d for d in dets if d.nearby]
    if not hits:
        return None
    cfg = app.cfg
    units = {"reflectivity": "dBZ", "preciprate": "mm/h"}
    detail = ", ".join(
        f"{d.product} {d.max_value:.0f} {units[d.product]} {d.describe()}" for d in hits
    )
    eta: float | None = None
    if cfg.direction_filter:
        refl = next((d for d in hits if d.product == "reflectivity"), None)
        refl_frames = app.radar.frames("reflectivity") if refl is not None else []
        m = estimate(refl_frames) if refl is not None else None
        if refl is not None and m is not None:
            nearest_hit = min(hits, key=lambda d: d.nearest_km)
            a = project(nearest_hit, m,
                        hit_radius_km=max(cfg.now_radius_km, cfg.alert_radius_km / 4),
                        lookahead_min=cfg.lookahead_min)
            if not a.will_hit:
                log.info(
                    "radar echo moving away (closest approach %.1f km, motion %.1f/%.1f "
                    "km/min, conf %.2f); not alerting",
                    a.closest_km, m.u_km_per_min, m.v_km_per_min, m.confidence,
                )
                app.last_note = "moving away"
                return None
            # The projection runs from the frame's valid time; count down the frame's age so
            # the message says how long from now. will_hit above stays on the raw projection.
            age_min = (now - refl_frames[-1].valid_time).total_seconds() / 60
            eta = max(0.0, (a.eta_min or 0.0) - age_min)
            detail += f", eta {eta:.0f} min"
        elif cfg.debug >= 1:
            log.info("DEBUG direction filter: motion unknown, plain radius alerting")
    return Trigger(source="radar", eta_min=eta, detail=detail)


def _maybe_send_test(app: App) -> None:
    if app.cfg.debug >= 2 and not app.state.test_sent():
        if app.notifier.send(TITLE_TEST, "Test from astrorainprotect; ntfy path works."):
            app.state.mark_test_sent()
            log.info("TEST notification sent")
        else:
            app.failures["ntfy"] += 1
            log.error(
                "ERROR ntfy: test send failed (consecutive failures: %d)", app.failures["ntfy"]
            )


def run_cycle(app: App) -> str:
    cfg, now = app.cfg, app.clock()
    ts = now.timestamp()
    app.last_note = ""

    if cfg.scope_hosts:
        online = app.scope_check()
        if not online:
            app.state.clear_latch()
            line = f"no scope online ({cfg.scope_hosts}), not checking"
            log.info(line)
            app.state.heartbeat(ts)
            return line
        if cfg.debug >= 1:
            log.info("DEBUG scope(s) online: %s", " ".join(online))

    _maybe_send_test(app)

    dets: list[Detection] = []
    statuses: dict[str, RadarStatus] = {}
    newest: Frame | None = None
    fetch_errors = 0
    for product in ("reflectivity", "preciprate"):
        # Per product: one product failing must not discard the other's trigger.
        try:
            d, status = _detect_product(app, product, now)
        except MrmsError as exc:
            fetch_errors += 1
            statuses[product] = RadarStatus(False, f"fetch failed: {exc}")
            app.failures["radar"] += 1
            log.error("ERROR radar %s: %s (consecutive failures: %d)",
                      product, exc, app.failures["radar"])
            continue
        statuses[product] = status
        if d is not None:
            dets.append(d)
        frames = app.radar.frames(product)
        if frames and (newest is None or frames[-1].valid_time > newest.valid_time):
            newest = frames[-1]
    if fetch_errors == 0:
        app.failures["radar"] = 0

    radar_ok = len(statuses) == 2 and all(s.available for s in statuses.values())
    if not radar_ok:
        unavailable = [f"{p}: {s.reason}" for p, s in statuses.items() if not s.available]
        reasons = "; ".join(unavailable) or "fetch failed"
        log.warning("radar unavailable (%s); latch untouched", reasons)

    triggers: list[Trigger] = []
    rt = radar_trigger(app, dets, now)
    if rt:
        triggers.append(rt)
    wet = next((d for d in dets if d.product == "preciprate" and d.raining_now), None)
    raining_now = wet is not None
    if wet is not None:
        # Rain forming in place over the house must alert (differs from the legacy re-arm).
        triggers.append(Trigger(source="radar", eta_min=0.0,
                                detail=f"raining at the house now ({wet.rate_at_house:.1f} mm/h)"))

    if app.pirate is not None:
        try:
            pr = app.pirate.check(now)
            app.failures["pirate"] = 0
        except PirateError as exc:
            app.failures["pirate"] += 1
            log.error(
                "ERROR pirate weather: %s (consecutive failures: %d)",
                exc, app.failures["pirate"],
            )
            pr = None
        if pr is not None:
            if cfg.debug >= 1:
                log.info("DEBUG pirate: %s", pr.summary())
            if pr.eta_min is not None:
                triggers.append(Trigger("pirate weather", float(pr.eta_min), pr.detail))

    if radar_ok or triggers:
        decision = decide(AlarmInputs(tuple(triggers), app.state.latched(),
                                      app.state.latch_age_sec(ts), cfg.repeat_min))
    else:
        decision = decide(AlarmInputs((), False, None, cfg.repeat_min))   # NONE

    outcome = decision.action.name.lower()
    if decision.action in (Action.SEND, Action.REPEAT):
        if app.notifier.send(decision.title, decision.message):
            app.state.set_latch(ts)
            app.failures["ntfy"] = 0
            log.info(
                "%s: %s",
                "ALERT sent" if decision.action is Action.SEND else "REPEAT alert sent",
                decision.message,
            )
        else:
            app.failures["ntfy"] += 1
            log.error(
                "ERROR ntfy: send failed (consecutive failures: %d)", app.failures["ntfy"]
            )
            outcome = "send-failed"
    elif decision.action is Action.REARM:
        app.state.clear_latch()
        outcome = "re-armed"

    frame_txt = "none"
    age_txt = "n/a"
    if newest is not None:
        frame_txt = newest.valid_time.strftime("%H:%M:%SZ")
        age_txt = f"{(now - newest.valid_time).total_seconds() / 60:.1f}min"
    per_product = " ".join(
        f"{d.product}=max:{d.max_value:.1f},cells:{d.qualifying_cells},nearest:{d.describe()}"
        for d in dets
    ) or "radar=unavailable"
    eta_txt = min([t.eta_min for t in triggers if t.eta_min is not None], default=None)
    line = (f"frame={frame_txt} age={age_txt} {per_product} raining_now={int(raining_now)} "
            f"eta={'none' if eta_txt is None else f'{eta_txt:.0f}min'} "
            f"sources={','.join(t.source for t in triggers) or 'none'} "
            f"latched={int(app.state.latched())} outcome={outcome} note={app.last_note or '-'}")
    log.info(line)
    app.state.heartbeat(ts)
    return line


def _setup_logging(debug: int) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_app(cfg: Config) -> App:
    client = httpx.Client(headers={"User-Agent": "astrorainprotect"})
    hosts = parse_hosts(cfg.scope_hosts)
    return App(
        cfg=cfg,
        radar=RadarSource(client, cfg.lat, cfg.lon),
        notifier=Notifier(client, cfg.ntfy_url, cfg.ntfy_token, cfg.ntfy_priority),
        state=State(cfg.state_dir),
        scope_check=lambda: online_hosts(hosts),
        pirate=(PirateSource(client, cfg.pw_key, cfg.lat, cfg.lon, lookahead_min=cfg.lookahead_min,
                             min_prob=cfg.min_prob, min_intensity=cfg.min_intensity,
                             raining_now=cfg.raining_now) if cfg.pw_key else None),
        clock=lambda: datetime.now(UTC),
    )


def main(argv: list[str] | None = None) -> int:
    try:
        cfg = load_config(os.environ)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    _setup_logging(cfg.debug)
    log.info("astrorainprotect starting\n%s", describe(cfg))
    if cfg.replay_dir:
        from astrorainprotect.replay import run_replay
        run_replay(cfg, cfg.replay_dir)
        return 0
    app = build_app(cfg)
    app.state.clear_test_marker()

    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    while not stop["flag"]:
        try:
            run_cycle(app)
        except Exception:  # never let one bad cycle kill the alarm
            log.exception("unexpected error in poll cycle")
        for _ in range(cfg.poll_sec):
            if stop["flag"]:
                break
            time.sleep(1)
    log.info("stopped")
    return 0
