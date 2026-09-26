"""Poll loop and wiring."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from astrorainprotect.alarm import HOUSE, Action, AlarmInputs, Trigger, decide
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
RADAR_FAILURE_KINDS = ("listing", "download", "decode", "fetch")


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
            self.failures = {f"radar.{k}": 0 for k in RADAR_FAILURE_KINDS}
            self.failures.update(pirate=0, ntfy=0)


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
                app.last_note = "reflectivity moving away"
                return None
            # The projection runs from the frame's valid time; count down the frame's age so
            # the message says how long from now. will_hit above stays on the raw projection.
            age_min = (now - refl_frames[-1].valid_time).total_seconds() / 60
            eta = max(0.0, (a.eta_min or 0.0) - age_min)
            detail += f", eta {eta:.0f} min"
        elif cfg.debug >= 1:
            log.info("DEBUG direction filter: motion unknown, plain radius alerting")
    return Trigger(source="radar", eta_min=eta, detail=detail)


def _scope_status(cfg: Config, online: list[str] | None) -> str:
    """Human text for the test notification: what the scope gate will do this cycle."""
    if online is None:
        return "Scope gate disabled; checking every poll."
    if online:
        return f"Scopes online: {' '.join(online)}"
    return f"No scope online ({cfg.scope_hosts}); waiting for one before checking radar."


def _announce_scope_changes(app: App, online: list[str]) -> None:
    """Issue #22: one notification per poll when any scope host comes or goes.
    The last known set lives in the state dir, so a restart does not re-announce. The set is
    recorded only after a successful send, so a failed announcement is retried next poll."""
    now_online = set(online)
    before = app.state.last_scopes()
    if before is None:
        app.state.set_scopes(now_online)
        return
    if before == now_online:
        return
    came = sorted(now_online - before)
    went = sorted(before - now_online)
    parts = []
    if came:
        parts.append(f"{', '.join(came)} came online")
    if went:
        parts.append(f"{', '.join(went)} went offline")
    if now_online:
        tail = f"Online: {', '.join(sorted(now_online))}. Radar checks active."
    else:
        tail = "No scope online; radar checks paused until one returns."
    title = "Scopes changed" if came and went else ("Scope online" if came else "Scope offline")
    if app.notifier.send(title, f"{'; '.join(parts)}. {tail}", priority="default"):
        log.info("scope change announced: %s", "; ".join(parts))
        app.state.set_scopes(now_online)
    else:
        app.failures["ntfy"] += 1
        log.error("ntfy: scope announcement failed (consecutive failures: %d); will retry",
                  app.failures["ntfy"])


STARTUP_FAILURE_MARKER = "astrorainprotect_startup_failure_sent"


def notify_startup_failure(env: Mapping[str, str], reason: str, tmp_dir: Path | str = "/tmp",
                           client: httpx.Client | None = None) -> bool:
    """Issue #26: tell the phone once per container lifetime that startup failed."""
    url = (env.get("NTFY_URL") or "").strip()
    if not url:
        return False
    marker = Path(tmp_dir) / STARTUP_FAILURE_MARKER
    if marker.exists():
        return False
    token = (env.get("NTFY_TOKEN") or "").strip()
    notifier = Notifier(client or httpx.Client(), url, token, "high")
    try:
        sent = notifier.send("astrorainprotect failed to start", f"{reason} The container will "
                             "keep restarting until this is fixed; no rain alerts until then.")
    except Exception as exc:  # e.g. an unparseable NTFY_URL is itself a config error
        print(f"could not send the startup-failure notification: {exc}", file=sys.stderr)
        return False
    if sent:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.touch()
        except OSError:
            pass
    return sent


def _maybe_send_test(app: App, scope_status: str) -> None:
    if app.cfg.debug >= 2 and not app.state.test_sent():
        if app.notifier.send(TITLE_TEST, f"Test from astrorainprotect. {scope_status}"):
            app.state.mark_test_sent()
            log.info("TEST notification sent")
        else:
            app.failures["ntfy"] += 1
            log.error(
                "ntfy: test send failed (consecutive failures: %d)", app.failures["ntfy"]
            )


def run_cycle(app: App) -> str:
    cfg, now = app.cfg, app.clock()
    ts = now.timestamp()
    app.last_note = ""

    # The DEBUG=2 test send runs before the scope gate so a restart always announces itself,
    # and says whether it is checking radar or waiting for a scope.
    online = app.scope_check() if cfg.scope_hosts else None
    _maybe_send_test(app, _scope_status(cfg, online))
    if online is not None:
        try:
            _announce_scope_changes(app, online)
        except Exception:   # informational only; must never block the radar path
            log.exception("scope announcement failed")

    if online is not None:
        if not online:
            app.state.clear_latch()
            line = (f"frame=none age=n/a radar=skipped raining_now=0 eta=none sources=none "
                    f"latched=0 outcome=scope-offline note=no scope online ({cfg.scope_hosts})")
            log.info(line)
            app.state.heartbeat(ts)
            return line
        if cfg.debug >= 1:
            log.info("DEBUG scope(s) online: %s", " ".join(online))

    dets: list[Detection] = []
    statuses: dict[str, RadarStatus] = {}
    newest: Frame | None = None
    failed_kinds: set[str] = set()
    for product in ("reflectivity", "preciprate"):
        # Per product: one product failing must not discard the other's trigger.
        try:
            d, status = _detect_product(app, product, now)
        except MrmsError as exc:
            kind = exc.kind if exc.kind in RADAR_FAILURE_KINDS else "fetch"
            failed_kinds.add(kind)
            statuses[product] = RadarStatus(False, f"fetch failed: {exc}")
            app.failures[f"radar.{kind}"] += 1
            log.error("radar %s %s: %s (consecutive failures: %d)",
                      kind, product, exc, app.failures[f"radar.{kind}"])
            continue
        statuses[product] = status
        if d is not None:
            dets.append(d)
        frames = app.radar.frames(product)
        if frames and (newest is None or frames[-1].valid_time > newest.valid_time):
            newest = frames[-1]
    for kind in RADAR_FAILURE_KINDS:
        if kind not in failed_kinds:
            app.failures[f"radar.{kind}"] = 0

    radar_ok = len(statuses) == 2 and all(s.available for s in statuses.values())
    if not radar_ok:
        unavailable = [f"{p}: {s.reason}" for p, s in statuses.items() if not s.available]
        reasons = "; ".join(unavailable) or "fetch failed"
        log.warning("radar unavailable (%s); latch untouched", reasons)

    triggers: list[Trigger] = []
    wet = next((d for d in dets if d.product == "preciprate" and d.raining_now), None)
    raining_now = wet is not None
    # When it is raining at the house the "house" trigger describes PrecipRate; the PrecipRate
    # "nearby" detection would only repeat the same cell, so only reflectivity feeds the radar
    # trigger in that case.
    radar_dets = [d for d in dets if not (raining_now and d.product == "preciprate")]
    rt = radar_trigger(app, radar_dets, now)
    if rt:
        triggers.append(rt)
    if wet is not None:
        # Rain forming in place over the house must alert (differs from the legacy re-arm).
        triggers.append(Trigger(source=HOUSE, eta_min=0.0, detail=f"{wet.rate_at_house:.1f} mm/h"))

    if app.pirate is not None:
        try:
            pr = app.pirate.check(now)
            app.failures["pirate"] = 0
        except PirateError as exc:
            app.failures["pirate"] += 1
            log.error(
                "pirate weather: %s (consecutive failures: %d)",
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
                "ntfy: send failed (consecutive failures: %d)", app.failures["ntfy"]
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
        notify_startup_failure(os.environ, f"Config error: {exc}.", "/tmp")
        return 2
    _setup_logging(cfg.debug)
    log.info("astrorainprotect starting\n%s", describe(cfg))
    if cfg.replay_dir:
        from astrorainprotect.replay import run_replay
        run_replay(cfg, cfg.replay_dir)
        return 0
    try:
        State(cfg.state_dir).heartbeat(time.time())   # fail fast if the volume is not ours
    except OSError as exc:
        reason = (f"STATE_DIR {cfg.state_dir} is not writable ({exc}). The container runs as "
                  f"uid 1000; create the directory and chown 1000:1000 it.")
        print(reason, file=sys.stderr)
        notify_startup_failure(os.environ, reason, "/tmp")
        return 3
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
