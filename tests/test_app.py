import logging
import re
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.app import RADAR_MAX_AGE, App, radar_status, run_cycle
from astrorainprotect.config import load_config
from astrorainprotect.pirate import PirateError, PirateResult
from astrorainprotect.state import State
from tests.conftest import make_frame

NOW = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


class FakeRadar:
    def __init__(self, preciprate=None, reflectivity=None, error=None, error_for=None):
        self.by_product = {"preciprate": preciprate, "reflectivity": reflectivity}
        self.error = error
        self.error_for = error_for            # raise only for this product, when set
        self.calls = 0

    def fetch_latest(self, product, now):
        self.calls += 1
        if self.error and (self.error_for is None or self.error_for == product):
            raise self.error
        return self.by_product[product]

    def frames(self, product):
        f = self.by_product[product]
        return [f] if f else []


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok, self.sent, self.priorities = ok, [], []

    def send(self, title, message, priority=None):
        self.sent.append((title, message))
        self.priorities.append(priority)
        return self.ok


def empty(product):
    return make_frame(np.zeros((101, 101)), product=product, valid_time=NOW - timedelta(minutes=2))


def storm(product, value):
    g = np.zeros((101, 101), dtype=np.float32)
    g[60:64, 40:44] = value                      # ~11 km SW
    return make_frame(g, product=product, valid_time=NOW - timedelta(minutes=2))


def raining(product, value):
    g = np.zeros((101, 101), dtype=np.float32)
    g[49:52, 49:52] = value
    return make_frame(g, product=product, valid_time=NOW - timedelta(minutes=2))


def build(tmp_path, env=None, radar=None, notifier=None, scope=None, now=NOW):
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path / "state"), **(env or {})})
    return App(cfg=cfg, radar=radar or FakeRadar(empty("preciprate"), empty("reflectivity")),
               notifier=notifier or FakeNotifier(), state=State(cfg.state_dir, tmp_path / "tmp"),
               scope_check=scope or (lambda: []), pirate=None, clock=lambda: now)


def test_radar_status():
    assert radar_status(None, NOW).available is False
    assert radar_status(empty("preciprate"), NOW).available is True
    old = make_frame(np.zeros((3, 3)), valid_time=NOW - RADAR_MAX_AGE - timedelta(seconds=1))
    old_status = radar_status(old, NOW)
    assert old_status.available is False and "stale" in old_status.reason
    gappy = make_frame(np.zeros((3, 3)), valid_time=NOW, missing_fraction=0.6)
    gappy_status = radar_status(gappy, NOW)
    assert gappy_status.available is False and "missing" in gappy_status.reason


def test_quiet_cycle_logs_summary(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    app = build(tmp_path)
    line = run_cycle(app)
    assert "frame=" in line and "age=" in line and "latched=0" in line
    assert app.notifier.sent == []
    assert app.state.heartbeat_age_sec(NOW.timestamp()) == 0.0


def test_reflectivity_storm_sends_alert(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    title, msg = app.notifier.sent[0]
    assert title == "Rain incoming"
    assert "SW" in msg and "reflectivity" in msg
    assert app.state.latched()


def test_preciprate_storm_sends_alert(tmp_path):
    app = build(tmp_path, radar=FakeRadar(storm("preciprate", 1.0), empty("reflectivity")))
    run_cycle(app)
    assert app.notifier.sent[0][0] == "Rain incoming"
    assert "preciprate" in app.notifier.sent[0][1]


def test_second_cycle_skips(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    run_cycle(app)
    assert len(app.notifier.sent) == 1


def test_repeat(tmp_path):
    app = build(tmp_path, env={"REPEAT_MIN": "10"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    app.clock = lambda: NOW + timedelta(minutes=11)
    old_values = app.radar.by_product["reflectivity"].values
    app.radar.by_product["reflectivity"] = make_frame(
        old_values, product="reflectivity", valid_time=NOW + timedelta(minutes=9)
    )
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming", "Rain incoming (still)"]


def test_send_failure_does_not_latch(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)),
                notifier=FakeNotifier(ok=False))
    run_cycle(app)
    assert not app.state.latched()
    run_cycle(app)
    assert len(app.notifier.sent) == 2                # retried next poll
    assert any("consecutive failures: 2" in r.getMessage() for r in caplog.records)


def test_rain_at_house_alerts_when_unlatched(tmp_path):
    app = build(tmp_path, radar=FakeRadar(raining("preciprate", 1.0), empty("reflectivity")))
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1
    title, msg = app.notifier.sent[0]
    assert title == "Currently raining"
    assert msg.startswith("Currently raining at the house (1.0 mm/h)")
    assert "preciprate" not in msg                     # the house cell is not listed twice
    assert app.state.latched()
    assert "raining_now=1" in line and "sources=house" in line


def test_rain_at_house_while_latched_skips(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar.by_product["preciprate"] = raining("preciprate", 1.0)
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1
    assert app.state.latched()
    assert "outcome=skip" in line


def test_rearms_after_everything_clears(tmp_path):
    app = build(tmp_path, radar=FakeRadar(raining("preciprate", 1.0), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar.by_product["preciprate"] = empty("preciprate")
    app.radar.by_product["reflectivity"] = empty("reflectivity")
    line = run_cycle(app)
    assert not app.state.latched()
    assert "outcome=re-armed" in line


def test_radar_unavailable_leaves_latch(tmp_path, caplog):
    caplog.set_level(logging.WARNING)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar = FakeRadar(None, None)
    run_cycle(app)
    assert app.state.latched()
    assert any("radar unavailable" in r.getMessage() for r in caplog.records)


def test_radar_partial_outage_leaves_latch(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar = FakeRadar(empty("preciprate"), None)
    run_cycle(app)
    assert app.state.latched()
    assert len(app.notifier.sent) == 1                # nothing new was sent


def test_radar_error_is_logged_and_loop_continues(tmp_path, caplog):
    from astrorainprotect.mrms import MrmsError
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(error=MrmsError("S3 listing failed")))
    line = run_cycle(app)
    assert any("S3 listing failed" in r.getMessage() for r in caplog.records)
    assert "radar=unavailable" in line


def test_one_product_error_keeps_other_trigger(tmp_path, caplog):
    from astrorainprotect.mrms import MrmsError
    caplog.set_level(logging.ERROR)
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0),
                      error=MrmsError("preciprate GET failed", kind="download"),
                      error_for="preciprate")
    app = build(tmp_path, radar=radar)
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("preciprate GET failed" in m and "consecutive failures: 1" in m for m in errors)
    assert app.failures["radar.download"] == 1


def test_scope_offline_skips_and_clears_latch(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, radar=radar, scope=lambda: [])
    app.state.set_latch(NOW.timestamp())
    line = run_cycle(app)
    assert radar.calls == 0
    assert not app.state.latched()
    assert "no scope online" in line
    assert app.notifier.sent == []
    assert app.state.heartbeat_age_sec(NOW.timestamp()) == 0.0


def test_scope_online_runs_checks(tmp_path):
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, radar=radar, scope=lambda: ["10.0.0.5"])
    run_cycle(app)
    assert radar.calls == 2 and len(app.notifier.sent) == 1


def test_debug2_sends_one_test_notification(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2"})
    run_cycle(app)
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test"]


def test_debug2_test_send_runs_even_when_scope_offline(tmp_path):
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"DEBUG": "2", "SCOPE_HOSTS": "10.0.0.5,10.0.0.6"}, radar=radar,
                scope=lambda: [])
    app.state.set_latch(NOW.timestamp())
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test"]
    assert "No scope online (10.0.0.5,10.0.0.6); waiting" in app.notifier.sent[0][1]
    assert radar.calls == 0                            # the gate still skipped the checks
    assert not app.state.latched()                     # and still reset the latch


def test_debug2_test_message_names_online_scopes(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2", "SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: ["10.0.0.5"])
    run_cycle(app)
    assert "Scopes online: 10.0.0.5" in app.notifier.sent[0][1]


def test_debug2_test_message_without_scope_gate(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2"})
    run_cycle(app)
    assert "Scope gate disabled" in app.notifier.sent[0][1]


def test_debug2_test_marker_cleared_by_main_start(tmp_path):
    app = build(tmp_path, env={"DEBUG": "2"})
    app.state.mark_test_sent()
    app.state.clear_test_marker()
    run_cycle(app)
    assert app.notifier.sent[0][0] == "Rain alert test"


def test_main_missing_env_exits_nonzero(monkeypatch, capsys):
    from astrorainprotect.app import main
    monkeypatch.setattr("os.environ", {})
    assert main([]) == 2
    assert "LAT is required" in capsys.readouterr().err


class FakePirate:
    def __init__(self, result=None, error=None):
        self.result, self.error, self.calls = result, error, 0

    def check(self, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.result


def pw(eta, raining_now=False):
    return PirateResult(raining_now, 0.0, 70, 1.2, eta, 60, "prob 70%", 60)


def test_pirate_trigger_alone_sends(tmp_path):
    app = build(tmp_path)
    app.pirate = FakePirate(pw(15))
    run_cycle(app)
    assert app.notifier.sent[0][1].startswith("Rain expected in about 15 min")
    assert "pirate weather" in app.notifier.sent[0][1]


def test_pirate_error_logged_radar_still_alerts(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.pirate = FakePirate(error=PirateError("HTTP 429"))
    run_cycle(app)
    assert any("HTTP 429" in r.getMessage() for r in caplog.records)
    assert len(app.notifier.sent) == 1


def test_pirate_raining_now_does_not_silence_radar(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.pirate = FakePirate(pw(None, raining_now=True))
    line = run_cycle(app)
    assert app.state.latched()                        # PW "raining" changed nothing
    assert len(app.notifier.sent) == 1
    assert "outcome=skip" in line


def test_pirate_raining_now_does_not_veto_first_alert(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.pirate = FakePirate(pw(None, raining_now=True))
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]


def test_build_app_creates_pirate_only_with_key(tmp_path):
    from astrorainprotect.app import build_app
    from astrorainprotect.pirate import PirateSource
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path)})
    assert build_app(cfg).pirate is None
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path), "PW_KEY": "k"})
    assert isinstance(build_app(cfg).pirate, PirateSource)


def moving_storm(k, toward=True, age_min=0.0):
    """Reflectivity blob ~11 km SW that steps 3 cells per frame toward (or away from) the house.

    age_min shifts every frame back in time so the newest (k=2) is that many minutes old.
    """
    g = np.zeros((101, 101), dtype=np.float32)
    off = -k if toward else k                      # one cell per 2-minute frame
    g[62 + off:66 + off, 38 - off:42 - off] = 40.0  # starts ~14 km SW; stays inside 20 km
    return make_frame(g, product="reflectivity",
                      valid_time=NOW - timedelta(minutes=2 * (2 - k) + age_min))


class HistoryRadar(FakeRadar):
    def __init__(self, frames, preciprate=None):
        super().__init__(preciprate if preciprate is not None else empty("preciprate"), frames[-1])
        self._hist = frames

    def frames(self, product):
        return self._hist if product == "reflectivity" else [self.by_product["preciprate"]]


def test_direction_filter_suppresses_receding_storm(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert app.notifier.sent == []
    assert "moving away" in line


def test_direction_filter_alerts_with_eta_for_approaching_storm(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=True) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    assert app.notifier.sent[0][1].startswith("Rain expected in about")


def _eta_in_message(tmp_path, age_min):
    radar = HistoryRadar([moving_storm(k, toward=True, age_min=age_min) for k in range(3)])
    app = build(tmp_path / f"age{age_min}", env={"DIRECTION_FILTER": "1"}, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    msg = app.notifier.sent[0][1]
    m = re.match(r"Rain expected in about (\d+) min", msg)
    assert m, msg
    assert f"eta {m.group(1)} min" in msg          # detail carries the same adjusted ETA
    return int(m.group(1))


def test_eta_is_adjusted_for_frame_age(tmp_path):
    fresh = _eta_in_message(tmp_path, 0.0)
    aged = _eta_in_message(tmp_path, 4.0)
    assert aged < fresh
    assert abs((fresh - aged) - 4) <= 1               # rounding of both ETAs


def test_direction_filter_falls_back_without_history(tmp_path):
    # one frame only
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert len(app.notifier.sent) == 1                 # never suppress without motion data


def test_direction_filter_off_ignores_motion(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1


def test_direction_filter_does_not_suppress_preciprate_hit(tmp_path):
    # reflectivity recedes (would be suppressed alone), but a qualifying PrecipRate echo
    # ~4 km south of the house (outside NOW_RADIUS_KM, so not "raining now") must still alert.
    g = np.zeros((101, 101), dtype=np.float32)
    g[54:57, 50] = 1.0
    precip = make_frame(g, product="preciprate", valid_time=NOW - timedelta(minutes=2))
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)], preciprate=precip)
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1


def test_main_exits_when_state_dir_not_writable(monkeypatch, capsys, tmp_path):
    """Issue #14: discover an unwritable STATE_DIR at startup, not on the first alert."""
    import os as _os

    import astrorainprotect.app as appmod
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    if _os.access(ro, _os.W_OK):
        pytest.skip("running as root; permissions are not enforced")
    monkeypatch.setattr("os.environ", {**BASE, "STATE_DIR": str(ro)})
    monkeypatch.setattr(appmod, "build_app", lambda cfg: pytest.fail("loop must not start"))
    try:
        assert appmod.main([]) == 3
    finally:
        ro.chmod(0o700)
    err = capsys.readouterr().err
    assert "STATE_DIR" in err and str(ro) in err and "1000" in err


def test_radar_failure_counters_are_per_class(tmp_path, caplog):
    """Issue #20: listing, download and decode failures count separately."""
    from astrorainprotect.mrms import MrmsError
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(error=MrmsError("S3 listing failed", kind="listing")))
    run_cycle(app)
    run_cycle(app)
    assert app.failures["radar.listing"] == 4          # two products x two cycles
    assert app.failures.get("radar.download", 0) == 0
    app.radar = FakeRadar(error=MrmsError("GRIB decode failed", kind="decode"))
    run_cycle(app)
    assert app.failures["radar.listing"] == 0           # class with no error this cycle resets
    assert app.failures["radar.decode"] == 2
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("radar decode") and "consecutive failures: 2" in m for m in msgs)


def test_scope_offline_summary_line_has_full_field_set(tmp_path):
    """Issue #20: the scope-offline path logs the same field set as every other poll."""
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: [])
    line = run_cycle(app)
    for field in ("frame=none", "age=n/a", "radar=skipped", "raining_now=0", "eta=none",
                  "sources=none", "latched=0", "outcome=scope-offline", "note=no scope online"):
        assert field in line, field


def test_direction_filter_note_names_reflectivity(tmp_path):
    """Issue #19: the note says what moved away, so it reads right next to outcome=send."""
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)],
                         preciprate=raining("preciprate", 1.0))
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert "note=reflectivity moving away" in line and "outcome=send" in line


def test_module_entry_hides_debug_from_eckit(tmp_path):
    """Issue #13: DEBUG=1/2 must not switch on eckit's PRE-MAIN-DEBUG chatter (it reads DEBUG)."""
    import subprocess
    import sys as _sys
    env = {**BASE, "DEBUG": "2", "REPLAY_DIR": str(tmp_path), "STATE_DIR": str(tmp_path / "s"),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([_sys.executable, "-m", "astrorainprotect"], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "PRE-MAIN" not in out.stdout + out.stderr
    assert "DEBUG=2" in out.stdout                      # the app still saw its own DEBUG


# --- issue #22: scope online/offline announcements -----------------------------------------


def scoped(tmp_path, scope):
    return build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5,10.0.0.6"}, scope=scope)


def test_first_cycle_records_scopes_silently(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    assert app.notifier.sent == []
    assert app.state.last_scopes() == {"10.0.0.5"}


def test_scope_going_offline_notifies_default_priority(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5", "10.0.0.6"])
    run_cycle(app)
    app.scope_check = lambda: ["10.0.0.5"]
    run_cycle(app)
    assert app.notifier.sent == [
        ("Scope offline", "10.0.0.6 went offline. Online: 10.0.0.5. Radar checks active.")
    ]
    assert app.notifier.priorities[-1] == "default"
    assert app.state.last_scopes() == {"10.0.0.5"}


def test_last_scope_offline_says_checks_paused(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    app.scope_check = lambda: []
    line = run_cycle(app)
    assert app.notifier.sent == [("Scope offline", "10.0.0.5 went offline. No scope online; "
                                                   "radar checks paused until one returns.")]
    assert "outcome=scope-offline" in line


def test_scope_coming_online_notifies(tmp_path):
    app = scoped(tmp_path, lambda: [])
    run_cycle(app)
    app.scope_check = lambda: ["10.0.0.6"]
    run_cycle(app)
    assert app.notifier.sent == [
        ("Scope online", "10.0.0.6 came online. Online: 10.0.0.6. Radar checks active.")
    ]


def test_both_directions_in_one_cycle(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    app.scope_check = lambda: ["10.0.0.6"]
    run_cycle(app)
    assert app.notifier.sent == [("Scopes changed", "10.0.0.6 came online; 10.0.0.5 went offline. "
                                                    "Online: 10.0.0.6. Radar checks active.")]


def test_unchanged_scopes_stay_silent_and_no_gate_means_no_announcements(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    run_cycle(app)
    assert app.notifier.sent == []
    plain = build(tmp_path / "plain")
    run_cycle(plain)
    assert plain.notifier.sent == [] and plain.state.last_scopes() is None


def test_scope_set_is_persisted_and_compared_across_restart(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    assert (tmp_path / "state" / "scopes_online").read_text() == "10.0.0.5"
    same = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5,10.0.0.6"}, scope=lambda: ["10.0.0.5"])
    run_cycle(same)                                    # same state dir, same scopes: silent
    assert same.notifier.sent == []
    changed = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5,10.0.0.6"}, scope=lambda: [])
    run_cycle(changed)                                 # changed while we were down: announced
    assert [t for t, _ in changed.notifier.sent] == ["Scope offline"]


def test_failed_announcement_is_retried_next_poll(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    app.scope_check = lambda: []
    app.notifier.ok = False
    run_cycle(app)
    assert app.state.last_scopes() == {"10.0.0.5"}     # not recorded: the change is still pending
    assert app.failures["ntfy"] == 1
    app.notifier.ok = True
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Scope offline", "Scope offline"]
    assert app.state.last_scopes() == set()


def test_state_write_failure_never_blocks_radar(tmp_path, caplog):
    caplog.set_level(logging.ERROR)
    app = scoped(tmp_path, lambda: ["10.0.0.5"])

    def boom(hosts):
        raise OSError(28, "No space left on device")

    app.state.set_scopes = boom
    line = run_cycle(app)
    assert "outcome=" in line                          # the cycle completed
    assert any("scope announcement failed" in r.getMessage() for r in caplog.records)
    assert app.state.heartbeat_age_sec(NOW.timestamp()) == 0.0


def test_startup_failure_survives_unparseable_url(tmp_path, capsys):
    from astrorainprotect.app import notify_startup_failure
    assert notify_startup_failure({"NTFY_URL": "https://[::1"}, "bad", tmp_path) is False
    assert "could not send" in capsys.readouterr().err


# --- issue #26: fatal startup errors notify --------------------------------------------------


def test_startup_failure_notification_headers_and_marker(tmp_path):
    import httpx as _httpx

    from astrorainprotect.app import notify_startup_failure
    seen = []

    def handler(request):
        seen.append((dict(request.headers), request.content.decode()))
        return _httpx.Response(200)

    client = _httpx.Client(transport=_httpx.MockTransport(handler))
    env = {"NTFY_URL": "https://ntfy.example.net/rain", "NTFY_TOKEN": "tk_x"}
    assert notify_startup_failure(env, "LAT is required", tmp_path, client=client) is True
    assert notify_startup_failure(env, "LAT is required", tmp_path, client=client) is False  # once
    assert len(seen) == 1
    headers, body = seen[0]
    assert headers["title"] == "astrorainprotect failed to start"
    assert headers["priority"] == "high" and headers["authorization"] == "Bearer tk_x"
    assert "LAT is required" in body


def test_startup_failure_without_url_is_noop(tmp_path):
    from astrorainprotect.app import notify_startup_failure
    assert notify_startup_failure({}, "anything", tmp_path) is False


def test_main_config_error_notifies(monkeypatch, capsys, tmp_path):
    import astrorainprotect.app as appmod
    calls = []
    monkeypatch.setattr(appmod, "notify_startup_failure",
                        lambda env, reason, tmp_dir, client=None: calls.append(reason) or True)
    monkeypatch.setattr("os.environ", {"LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/r"})
    assert appmod.main([]) == 2
    assert calls and "LAT is required" in calls[0]


def test_main_state_dir_error_notifies(monkeypatch, capsys, tmp_path):
    import os as _os

    import astrorainprotect.app as appmod
    ro = tmp_path / "ro"
    ro.mkdir()
    ro.chmod(0o500)
    if _os.access(ro, _os.W_OK):
        pytest.skip("running as root; permissions are not enforced")
    calls = []
    monkeypatch.setattr(appmod, "notify_startup_failure",
                        lambda env, reason, tmp_dir, client=None: calls.append(reason) or True)
    monkeypatch.setattr("os.environ", {**BASE, "STATE_DIR": str(ro)})
    monkeypatch.setattr(appmod, "build_app", lambda cfg: pytest.fail("loop must not start"))
    try:
        assert appmod.main([]) == 3
    finally:
        ro.chmod(0o700)
    assert calls and "STATE_DIR" in calls[0]
