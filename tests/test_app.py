import logging
from datetime import UTC, datetime, timedelta

import numpy as np

from astrorainprotect.app import RADAR_MAX_AGE, App, radar_status, run_cycle
from astrorainprotect.config import load_config
from astrorainprotect.pirate import PirateError, PirateResult
from astrorainprotect.state import State
from tests.conftest import make_frame

NOW = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


class FakeRadar:
    def __init__(self, preciprate=None, reflectivity=None, error=None):
        self.by_product = {"preciprate": preciprate, "reflectivity": reflectivity}
        self.error = error
        self.calls = 0

    def fetch_latest(self, product, now):
        self.calls += 1
        if self.error:
            raise self.error
        return self.by_product[product]

    def frames(self, product):
        f = self.by_product[product]
        return [f] if f else []


class FakeNotifier:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def send(self, title, message):
        self.sent.append((title, message))
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


def test_raining_now_rearms(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.state.latched()
    app.radar.by_product["preciprate"] = raining("preciprate", 1.0)
    line = run_cycle(app)
    assert not app.state.latched()
    assert "re-armed" in line or "rearm" in line.lower()


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


def test_pirate_raining_now_rearms(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    app.pirate = FakePirate(pw(None, raining_now=True))
    run_cycle(app)
    assert not app.state.latched()


def test_build_app_creates_pirate_only_with_key(tmp_path):
    from astrorainprotect.app import build_app
    from astrorainprotect.pirate import PirateSource
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path)})
    assert build_app(cfg).pirate is None
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path), "PW_KEY": "k"})
    assert isinstance(build_app(cfg).pirate, PirateSource)
