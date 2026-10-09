import logging
import re
from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.app import RADAR_MAX_AGE, TITLE_TEST, App, radar_status, run_cycle
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
    """Records sends. The leading "[HH:MM]" stamp (#64) is split off into `stamps` so the
    message assertions read the text alone."""

    def __init__(self, ok=True):
        self.ok, self.sent, self.priorities, self.attachments = ok, [], [], []
        self.stamps = []

    def send(self, title, message, priority=None, attachment=None):
        m = re.match(r"\[(\d\d:\d\d)\] (.*)", message, re.S)
        self.stamps.append(m.group(1) if m else None)
        self.sent.append((title, m.group(2) if m else message))
        self.priorities.append(priority)
        self.attachments.append(attachment)
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


def build(tmp_path, env=None, radar=None, notifier=None, scope=None, now=NOW, probe=None):
    cfg = load_config({**BASE, "STATE_DIR": str(tmp_path / "state"), **(env or {})})
    return App(cfg=cfg, radar=radar or FakeRadar(empty("preciprate"), empty("reflectivity")),
               notifier=notifier or FakeNotifier(), state=State(cfg.state_dir, tmp_path / "tmp"),
               scope_check=scope or (lambda: []), pirate=None, clock=lambda: now,
               probe=probe or (lambda: False))


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


def cells(product, value, n):
    """n qualifying-size cells ~11 km SW, in a row."""
    g = np.zeros((101, 101), dtype=np.float32)
    g[60, 40:40 + n] = value
    return make_frame(g, product=product, valid_time=NOW - timedelta(minutes=2))


def test_message_names_the_product_that_did_not_qualify(tmp_path):
    """#46: both products appear every time; the qualifying one first, the other with its state."""
    app = build(tmp_path, radar=FakeRadar(storm("preciprate", 1.0), cells("reflectivity", 32.0, 1)))
    run_cycle(app)
    msg = app.notifier.sent[0][1]
    assert re.search(r"radar: preciprate 1 mm/h [\d.]+ km to the SW; "
                     r"reflectivity 32 dBZ, 1 cell, below threshold", msg), msg


def test_message_says_when_the_other_product_has_no_echo(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.notifier.sent[0][1].endswith("; preciprate none in range")


def test_message_says_when_the_other_product_is_under_threshold(tmp_path):
    app = build(tmp_path, radar=FakeRadar(storm("preciprate", 1.0), cells("reflectivity", 18.0, 5)))
    run_cycle(app)
    assert app.notifier.sent[0][1].endswith("; reflectivity 18 dBZ, under threshold")


def test_message_says_when_the_other_product_has_no_data(tmp_path):
    from astrorainprotect.mrms import MrmsError
    radar = FakeRadar(None, storm("reflectivity", 40.0),
                      error=MrmsError("S3 listing failed", kind="listing"), error_for="preciprate")
    app = build(tmp_path, radar=radar)
    run_cycle(app)
    assert app.notifier.sent[0][1].endswith("; preciprate no data")


def test_message_pluralises_cells(tmp_path):
    app = build(tmp_path, radar=FakeRadar(storm("preciprate", 1.0), cells("reflectivity", 31.0, 2)))
    run_cycle(app)
    assert "reflectivity 31 dBZ, 2 cells, below threshold" in app.notifier.sent[0][1]


def test_alert_starts_a_recording_under_the_state_dir(tmp_path):
    """#57: the frames behind an alert are saved for replay, from the cached history on."""
    from astrorainprotect.recorder import Recorder
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.recorder = Recorder(tmp_path / "state" / "recordings")
    run_cycle(app)
    folders = list((tmp_path / "state" / "recordings").iterdir())
    assert len(folders) == 1 and len(list(folders[0].glob("*.npz"))) == 2


def test_quiet_cycle_records_nothing(tmp_path):
    from astrorainprotect.recorder import Recorder
    app = build(tmp_path)
    app.recorder = Recorder(tmp_path / "state" / "recordings")
    run_cycle(app)
    assert not (tmp_path / "state" / "recordings").exists()


def test_scope_offline_closes_the_recording(tmp_path):
    from astrorainprotect.recorder import Recorder
    gate = {"online": ["10.0.0.5"]}
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: gate["online"],
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.recorder = Recorder(tmp_path / "state" / "recordings")
    run_cycle(app)
    assert app.recorder.active
    gate["online"] = []
    run_cycle(app)
    run_cycle(app)                                     # offline after two missed polls
    assert not app.recorder.active


def test_build_app_records_only_when_enabled(tmp_path):
    from astrorainprotect.app import build_app
    env = {**BASE, "STATE_DIR": str(tmp_path)}
    assert build_app(load_config(env)).recorder is not None
    assert build_app(load_config({**env, "AUTO_RECORD": "0"})).recorder is None


def test_alert_is_stamped_with_the_frame_time_in_local_tz(tmp_path):
    """#64: ntfy shows only the date after a few hours; the body carries HH:MM of the frame."""
    frame = storm("reflectivity", 40.0)                   # valid 19:58Z
    app = build(tmp_path, env={"TZ": "America/Chicago"},
                radar=FakeRadar(empty("preciprate"), frame))
    run_cycle(app)
    assert app.notifier.stamps == ["14:58"]              # frame time, not the 15:00 clock


def test_stamp_follows_tz(tmp_path):
    app = build(tmp_path, env={"TZ": "UTC"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.notifier.stamps == ["19:58"]


def test_announcements_are_stamped_with_the_send_time(tmp_path):
    app = build(tmp_path, env={"TZ": "UTC", "ASTRORAINPROTECT_DEBUG": "2"})
    run_cycle(app)                                        # test notification
    app.pirate = FakePirate(error=PirateError("timed out"))
    app.failures["pirate"] = 2
    run_cycle(app)                                        # Pirate Weather unavailable
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test", "Pirate Weather unavailable"]
    assert app.notifier.stamps == ["20:00", "20:00"]


def test_scope_announcement_is_stamped(tmp_path):
    app = build(tmp_path, env={"TZ": "UTC", "SCOPE_HOSTS": "10.0.0.5"},
                scope=lambda: ["10.0.0.5"])
    app.state.set_scopes(set())
    run_cycle(app)
    assert app.notifier.sent[0][0] == "Scope online" and app.notifier.stamps == ["20:00"]


def contact_age(app):
    return app.state.contact_age_sec(app.clock().timestamp())


def test_radar_fetch_counts_as_contact(tmp_path):
    """#62: anything external that answers refreshes the contact file the healthcheck reads."""
    app = build(tmp_path)
    run_cycle(app)
    assert contact_age(app) == 0


def test_blind_cycle_leaves_contact_untouched(tmp_path):
    """Radar down, no scope answering, probe failing, ntfy failing: the 2026-10-03 outage."""
    from astrorainprotect.mrms import MrmsError
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: [],
                radar=FakeRadar(error=MrmsError("S3 listing failed", kind="listing")),
                notifier=FakeNotifier(ok=False), probe=lambda: False)
    app.state.set_scopes({"10.0.0.5"})
    for _ in range(3):
        run_cycle(app)
    assert contact_age(app) is None


def test_gate_closed_cycle_probes_the_bucket_and_counts_success(tmp_path):
    calls = []
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: [],
                probe=lambda: calls.append(1) or True)
    run_cycle(app)
    assert calls == [1] and contact_age(app) == 0


def test_gate_open_cycle_does_not_probe(tmp_path):
    calls = []
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: ["10.0.0.5"],
                probe=lambda: calls.append(1) or True)
    run_cycle(app)
    assert calls == []


def test_scope_answering_counts_as_contact(tmp_path):
    from astrorainprotect.mrms import MrmsError
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: ["10.0.0.5"],
                radar=FakeRadar(error=MrmsError("S3 listing failed", kind="listing")),
                notifier=FakeNotifier(ok=False))
    app.state.set_scopes({"10.0.0.5"})
    run_cycle(app)
    assert contact_age(app) == 0


def test_successful_send_counts_as_contact(tmp_path):
    from astrorainprotect.mrms import MrmsError
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"},
                radar=FakeRadar(error=MrmsError("S3 listing failed", kind="listing")))
    run_cycle(app)                                        # only the test notification succeeds
    assert contact_age(app) == 0


def test_contact_write_failure_never_blocks_the_alert(tmp_path, caplog):
    """The contact file is bookkeeping for the healthcheck; a full disk must not cost a cycle."""
    caplog.set_level(logging.ERROR)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    real_touch = app.state._touch

    def touch(path, now):
        if path.name == "contact":
            raise OSError(28, "No space left on device")
        real_touch(path, now)

    app.state._touch = touch
    line = run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"] and "outcome=send" in line
    assert any("contact" in r.getMessage() for r in caplog.records)


def test_build_app_has_a_bucket_probe(tmp_path):
    from astrorainprotect.app import build_app
    app = build_app(load_config({**BASE, "STATE_DIR": str(tmp_path)}))
    assert callable(app.probe)


def test_second_cycle_skips(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    run_cycle(app)
    assert len(app.notifier.sent) == 1


def test_repeat(tmp_path):
    # #53: two polls at 290 s (clock drift) are 580 s apart; the repeat must not slip a poll
    app = build(tmp_path, env={"REPEAT_MIN": "10", "POLL_SEC": "300"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    app.clock = lambda: NOW + timedelta(seconds=580)
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
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"})
    run_cycle(app)
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test"]


def test_debug2_test_send_runs_even_when_scope_offline(tmp_path):
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2", "SCOPE_HOSTS": "10.0.0.5,10.0.0.6"},
                radar=radar, scope=lambda: [])
    app.state.set_latch(NOW.timestamp())
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain alert test"]
    assert "No scope online (10.0.0.5,10.0.0.6); waiting" in app.notifier.sent[0][1]
    assert radar.calls == 0                            # the gate still skipped the checks
    assert not app.state.latched()                     # and still reset the latch


def test_debug2_test_message_names_online_scopes(tmp_path):
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2", "SCOPE_HOSTS": "10.0.0.5"},
                scope=lambda: ["10.0.0.5"])
    run_cycle(app)
    assert "Scopes online: 10.0.0.5" in app.notifier.sent[0][1]


def test_debug2_test_message_without_scope_gate(tmp_path):
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"})
    run_cycle(app)
    assert "Scope gate disabled" in app.notifier.sent[0][1]


def test_debug2_test_marker_cleared_by_main_start(tmp_path):
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"})
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


def test_summary_line_reports_pirate_state(tmp_path):
    """#51: the summary line says whether the secondary source answered."""
    app = build(tmp_path)
    assert "pirate=off" in run_cycle(app)
    app.pirate = FakePirate(result=pw(None))
    assert "pirate=ok" in run_cycle(app)
    app.pirate = FakePirate(error=PirateError("timed out"))
    assert "pirate=down:1" in run_cycle(app)
    assert "pirate=down:2" in run_cycle(app)


def test_pirate_outage_is_announced_once_and_recovery_once(tmp_path):
    app = build(tmp_path)
    app.pirate = FakePirate(error=PirateError("timed out"))
    for _ in range(5):
        run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Pirate Weather unavailable"]
    assert app.notifier.priorities == ["default"]
    assert "3 polls" in app.notifier.sent[0][1]
    app.pirate = FakePirate(result=pw(None))
    run_cycle(app)
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Pirate Weather unavailable",
                                                 "Pirate Weather back"]


def test_short_pirate_outage_is_silent(tmp_path):
    app = build(tmp_path)
    app.pirate = FakePirate(error=PirateError("timed out"))
    run_cycle(app)
    run_cycle(app)
    app.pirate = FakePirate(result=pw(None))
    run_cycle(app)
    assert app.notifier.sent == []


def test_pirate_announcement_failure_does_not_block_radar(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.pirate = FakePirate(error=PirateError("timed out"))
    app.notifier.ok = False
    for _ in range(3):
        run_cycle(app)
    titles = [t for t, _ in app.notifier.sent]
    assert "Rain incoming" in titles and "Pirate Weather unavailable" in titles


def test_pirate_announcement_exception_does_not_block_radar(tmp_path):
    """Like the scope announcements: informational, so any error in it is logged and dropped."""
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    app.pirate = FakePirate(error=PirateError("timed out"))
    app.failures["pirate"] = 2                      # the next failure triggers the announcement
    real_send = app.notifier.send

    def send(title, message, priority=None, attachment=None):
        if title.startswith("Pirate Weather"):
            raise RuntimeError("boom")
        return real_send(title, message, priority, attachment)

    app.notifier.send = send
    line = run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    assert "outcome=send" in line


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


def moving_storm(k, toward=True, age_min=0.0, value=40.0):
    """Reflectivity blob to the SW that steps 2 cells per frame toward (or away from) the house.

    Frames are 4 minutes apart, so three of them span the 8-minute minimum baseline. age_min
    shifts every frame back in time so the newest (k=2) is that many minutes old.
    """
    g = np.zeros((101, 101), dtype=np.float32)
    off = (2 - 2 * k) if toward else (2 * k - 2)
    g[62 + off:68 + off, 36 - off:42 - off] = value  # ends ~13 km (toward) or ~19 km (away) SW
    return make_frame(g, product="reflectivity",
                      valid_time=NOW - timedelta(minutes=4 * (2 - k) + age_min))


class HistoryRadar(FakeRadar):
    def __init__(self, frames, preciprate=None):
        super().__init__(preciprate if preciprate is not None else empty("preciprate"), frames[-1])
        self._hist = frames

    def frames(self, product):
        return self._hist if product == "reflectivity" else [self.by_product["preciprate"]]


def test_direction_filter_alerts_once_on_a_receding_storm_and_holds_repeats(tmp_path):
    """#48: the first alert always goes out; while the storm recedes, repeats are held."""
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1", "REPEAT_MIN": "10"}, radar=radar)
    line = run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    assert app.notifier.sent[0][1].startswith("Rain nearby (moving away): radar: ")
    assert re.search(r"moving away, closest approach [\d.]+ km", app.notifier.sent[0][1])
    assert "note=reflectivity moving away (repeats held)" in line and "outcome=send" in line
    assert "eta=none" in line and "eta " not in app.notifier.sent[0][1]   # receding: no ETA
    app.clock = lambda: NOW + timedelta(minutes=11)
    radar._hist = [moving_storm(k, toward=False, age_min=-11.0) for k in range(3)]
    radar.by_product["reflectivity"] = radar._hist[-1]
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1 and "outcome=skip" in line and app.state.latched()


def test_repeat_resumes_without_a_fresh_alert_when_the_storm_turns(tmp_path):
    """#48: suppression used to re-arm the latch, so every flip sent "Rain incoming" again."""
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1", "REPEAT_MIN": "10"}, radar=radar)
    run_cycle(app)
    app.clock = lambda: NOW + timedelta(minutes=11)
    radar._hist = [moving_storm(k, toward=True, age_min=-11.0) for k in range(3)]
    radar.by_product["reflectivity"] = radar._hist[-1]
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming", "Rain incoming (still)"]
    assert app.notifier.sent[1][1].startswith("Rain expected in about")


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


def crosswise_storm(k):
    """Two reflectivity blobs moving east two cells per 4-minute frame (issue #11).

    Blob A, ~8 km S, is the nearest echo but passes ~8 km south of the house.
    Blob B, ~12 km W, is farther but heads straight for the house.
    """
    g = np.zeros((101, 101), dtype=np.float32)
    g[57:61, 44 + 2 * k:49 + 2 * k] = 40.0
    g[48:52, 30 + 2 * k:35 + 2 * k] = 40.0
    return make_frame(g, product="reflectivity", valid_time=NOW - timedelta(minutes=4 * (2 - k)))


def test_direction_filter_projects_every_cell(tmp_path):
    radar = HistoryRadar([crosswise_storm(k) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1, line
    msg = app.notifier.sent[0][1]
    m = re.match(r"Rain expected in about (\d+) min", msg)
    assert m, msg
    assert 10 <= int(m.group(1)) <= 25

def test_arriving_now_detail_has_no_zero_eta(tmp_path):
    """Issue #32: when the ETA rounds to 0 the detail must not say 'eta 0 min'."""
    # hit radius 10 km: the echo 13 km out is about 4 minutes away, and the frame is 8 old
    radar = HistoryRadar([moving_storm(k, toward=True, age_min=8.0) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1", "ALERT_RADIUS_KM": "40"}, radar=radar)
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    msg = app.notifier.sent[0][1]
    assert msg.startswith("Rain arriving now ("), msg
    assert "eta" not in msg


def test_direction_filter_falls_back_without_history(tmp_path):
    # one frame only
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1                 # never suppress without motion data
    assert "note=motion unknown (no baseline)" in line  # #47: the log says why


def test_direction_filter_unknown_reason_is_logged_at_debug(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    # identical frames: the echo is there but has not moved
    radar = HistoryRadar([moving_storm(2, age_min=4.0 * (2 - k)) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1", "ASTRORAINPROTECT_DEBUG": "1"},
                radar=radar)
    line = run_cycle(app)
    assert "note=motion unknown (small shift)" in line
    assert any("motion unknown (small shift), plain radius alerting" in r.getMessage()
               for r in caplog.records)


def test_direction_filter_uses_reflectivity_motion_when_only_preciprate_qualifies(tmp_path):
    """#47: on the recorded storm PrecipRate often qualified while reflectivity in the radius
    was under MIN_DBZ; the reflectivity field across the box still shows where it is going."""
    frames = [moving_storm(k, toward=False, value=22.0) for k in range(3)]   # under MIN_DBZ 30
    precip = make_frame(np.where(frames[-1].values > 0, 1.0, 0.0), product="preciprate",
                        valid_time=NOW)
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"},
                radar=HistoryRadar(frames, preciprate=precip))
    line = run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    assert "note=preciprate moving away (repeats held)" in line


def test_direction_filter_error_falls_back_to_plain_alerting(tmp_path, monkeypatch, caplog):
    """The filter is on by default now; a bug in it must cost an ETA, never the alert."""
    import astrorainprotect.app as appmod
    caplog.set_level(logging.ERROR)
    monkeypatch.setattr(appmod, "estimate", lambda frames: 1 / 0)
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    assert "note=motion unknown (filter error)" in line and "outcome=send" in line
    assert any("direction filter failed" in r.getMessage() for r in caplog.records)


def test_direction_filter_off_ignores_motion(tmp_path):
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)])
    app = build(tmp_path, env={"DIRECTION_FILTER": "0"}, radar=radar)
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


def _precip_where(frame):
    return make_frame(np.where(frame.values > 0, 1.0, 0.0), product="preciprate", valid_time=NOW)


def test_direction_filter_ignores_stale_reflectivity_history(tmp_path):
    """The reflectivity fetch fails while PrecipRate qualifies: motion from cached frames that
    are no longer current must not drop the alert."""
    old = [moving_storm(k, toward=False, age_min=45.0) for k in range(3)]
    radar = HistoryRadar(old, preciprate=_precip_where(old[-1]))
    radar.by_product["reflectivity"] = None
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1, line
    assert "note=motion unknown (no current reflectivity)" in line


def test_direction_filter_needs_reflectivity_fetched_this_poll(tmp_path):
    """A history that was current one poll ago must not drop a PrecipRate alert once the
    reflectivity fetch fails."""
    from astrorainprotect.mrms import MrmsError
    recent = [moving_storm(k, toward=False, age_min=4.0) for k in range(3)]
    radar = HistoryRadar(recent, preciprate=_precip_where(recent[-1]))
    radar.error, radar.error_for = MrmsError("S3 listing failed", kind="listing"), "reflectivity"
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1, line
    assert "note=motion unknown (no current reflectivity)" in line


def test_direction_filter_without_any_reflectivity_frame(tmp_path):
    from astrorainprotect.mrms import MrmsError
    radar = FakeRadar(storm("preciprate", 1.0), None,
                      error=MrmsError("S3 listing failed", kind="listing"),
                      error_for="reflectivity")
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"}, radar=radar)
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1, line
    assert "note=motion unknown (no current reflectivity)" in line


def test_direction_filter_ignores_reflectivity_that_stopped_updating(tmp_path):
    # 12 minutes old: still inside RADAR_MAX_AGE, too old to say where the storm is going now
    lagging = [moving_storm(k, toward=False, age_min=12.0) for k in range(3)]
    app = build(tmp_path, env={"DIRECTION_FILTER": "1"},
                radar=HistoryRadar(lagging, preciprate=_precip_where(lagging[-1])))
    line = run_cycle(app)
    assert len(app.notifier.sent) == 1, line
    assert "note=motion unknown (no current reflectivity)" in line


def test_eta_is_the_earliest_cell_counted_from_its_own_frame(tmp_path):
    """Both products qualify; the PrecipRate cell is nearer, so it sets the ETA, and the age
    taken off is that of the PrecipRate frame."""
    def eta(precip_age):
        frames = [moving_storm(k) for k in range(3)]
        radar = HistoryRadar(frames)
        if precip_age is not None:
            g = np.zeros((101, 101), dtype=np.float32)
            g[59:62, 41:44] = 1.0                       # ~12 km SW, ahead of the reflectivity
            radar.by_product["preciprate"] = make_frame(
                g, product="preciprate", valid_time=NOW - timedelta(minutes=precip_age))
        app = build(tmp_path / f"p{precip_age}", env={"DIRECTION_FILTER": "1"}, radar=radar)
        run_cycle(app)
        return int(re.match(r"Rain expected in about (\d+) min", app.notifier.sent[0][1]).group(1))
    reflectivity_only, fresh, aged = eta(None), eta(0.0), eta(6.0)
    assert fresh < reflectivity_only
    assert abs((fresh - aged) - 6) <= 1


def test_eta_counts_from_the_frame_that_holds_the_echo(tmp_path):
    """Reflectivity is 8 minutes old but under MIN_DBZ in the radius; the PrecipRate frame
    that qualified is current, so its projection is not shortened by the reflectivity age."""
    def eta(refl_age):
        frames = [moving_storm(k, age_min=refl_age, value=22.0) for k in range(3)]
        app = build(tmp_path / f"a{refl_age}", env={"DIRECTION_FILTER": "1"},
                    radar=HistoryRadar(frames, preciprate=_precip_where(frames[-1])))
        run_cycle(app)
        return int(re.match(r"Rain expected in about (\d+) min", app.notifier.sent[0][1]).group(1))
    assert eta(8.0) == eta(0.0)


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
                  "sources=none", "pirate=skipped", "latched=0", "outcome=scope-offline",
                  "note=no scope online"):
        assert field in line, field


def test_rain_at_the_house_repeats_even_while_the_radar_echo_recedes(tmp_path):
    """Issue #19 / #48: the note says what moved away; the house trigger is never held."""
    radar = HistoryRadar([moving_storm(k, toward=False) for k in range(3)],
                         preciprate=raining("preciprate", 1.0))
    app = build(tmp_path, env={"DIRECTION_FILTER": "1", "REPEAT_MIN": "10"}, radar=radar)
    line = run_cycle(app)
    assert "note=reflectivity moving away (repeats held)" in line and "outcome=send" in line
    assert app.notifier.sent[0][0] == "Currently raining"
    app.clock = lambda: NOW + timedelta(minutes=11)
    radar._hist = [moving_storm(k, toward=False, age_min=-11.0) for k in range(3)]
    radar.by_product["reflectivity"] = radar._hist[-1]
    radar.by_product["preciprate"] = make_frame(raining("preciprate", 1.0).values,
                                                product="preciprate",
                                                valid_time=NOW + timedelta(minutes=9))
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Currently raining", "Currently raining (still)"]


def test_module_entry_does_not_read_debug(tmp_path):
    """Issue #25: DEBUG is eckit's variable now; the app reads ASTRORAINPROTECT_DEBUG only."""
    import subprocess
    import sys as _sys
    env = {**BASE, "ASTRORAINPROTECT_DEBUG": "2", "REPLAY_DIR": str(tmp_path),
           "STATE_DIR": str(tmp_path / "s"), "PATH": "/usr/bin:/bin"}
    out = subprocess.run([_sys.executable, "-m", "astrorainprotect"], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "PRE-MAIN" not in out.stdout + out.stderr
    assert "ASTRORAINPROTECT_DEBUG=2" in out.stdout


def test_legacy_debug_warns_at_startup(tmp_path):
    import subprocess
    import sys as _sys
    env = {**BASE, "DEBUG": "1", "REPLAY_DIR": str(tmp_path), "STATE_DIR": str(tmp_path / "s"),
           "PATH": "/usr/bin:/bin"}
    out = subprocess.run([_sys.executable, "-m", "astrorainprotect"], env=env, capture_output=True,
                         text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    assert "DEBUG is ignored" in out.stdout + out.stderr
    assert "ASTRORAINPROTECT_DEBUG" in out.stdout + out.stderr


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
    assert app.notifier.sent == []                     # one missed poll is a blip
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
    assert "outcome=scope-offline" not in run_cycle(app)   # first miss: gate stays open
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
    assert app.notifier.sent == [
        ("Scope online", "10.0.0.6 came online. Online: 10.0.0.5, 10.0.0.6. Radar checks active.")
    ]                                                  # .5 missed one poll: still counted online
    run_cycle(app)
    assert app.notifier.sent[1] == (
        "Scope offline", "10.0.0.5 went offline. Online: 10.0.0.6. Radar checks active.")


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
    run_cycle(changed)                                 # first miss after the restart: a blip
    assert changed.notifier.sent == []
    run_cycle(changed)                                 # second miss: announced
    assert [t for t, _ in changed.notifier.sent] == ["Scope offline"]


def test_failed_announcement_is_retried_next_poll(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    app.scope_check = lambda: []
    run_cycle(app)                                     # first miss: nothing to announce yet
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


# --- issue #16: radar snapshot attachment ---------------------------------------------------

def test_alert_carries_radar_snapshot(tmp_path):
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    data, name = app.notifier.attachments[0]
    assert name == "radar.png" and data[:8] == b"\x89PNG\r\n\x1a\n"


def test_snapshot_disabled(tmp_path):
    app = build(tmp_path, env={"SNAPSHOT": "0"},
                radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert app.notifier.attachments == [None]


def test_snapshot_failure_does_not_block_alert(tmp_path, monkeypatch):
    import astrorainprotect.app as appmod
    monkeypatch.setattr(appmod, "render", lambda *a, **k: 1 / 0)
    app = build(tmp_path, radar=FakeRadar(empty("preciprate"), storm("reflectivity", 40.0)))
    run_cycle(app)
    assert len(app.notifier.sent) == 1 and app.notifier.attachments == [None]


def test_test_notification_carries_snapshot(tmp_path):
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"},
                radar=FakeRadar(empty("preciprate"), empty("reflectivity")))
    run_cycle(app)
    assert app.notifier.sent[0][0] == TITLE_TEST
    assert app.notifier.attachments[0][1] == "radar.png"


def test_test_notification_without_radar_is_text_only(tmp_path):
    from astrorainprotect.mrms import MrmsError
    radar = FakeRadar(error=MrmsError("listing failed", kind="listing"))
    app = build(tmp_path, env={"ASTRORAINPROTECT_DEBUG": "2"}, radar=radar)
    run_cycle(app)
    assert app.notifier.sent[0][0] == TITLE_TEST
    assert app.notifier.attachments[0] is None


def test_stale_cached_frame_is_not_attached(tmp_path):
    """A radar outage leaves an old frame cached; a Pirate Weather alert must not show it."""
    old = make_frame(np.zeros((101, 101)), product="reflectivity",
                     valid_time=NOW - RADAR_MAX_AGE - timedelta(minutes=1))
    app = build(tmp_path, radar=FakeRadar(None, old))
    app.pirate = type("P", (), {"check": lambda self, now: pw(15)})()
    run_cycle(app)
    assert len(app.notifier.sent) == 1
    assert app.notifier.attachments == [None]


# --- scope debounce: one missed poll is a blip, two is offline ------------------------------

def test_single_missed_poll_is_silent_and_keeps_gate_open(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    radar = FakeRadar(empty("preciprate"), storm("reflectivity", 40.0))
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5", "ASTRORAINPROTECT_DEBUG": "1"},
                radar=radar, scope=lambda: ["10.0.0.5"])
    run_cycle(app)                                     # alert sent, latch set
    assert app.state.latched()
    app.scope_check = lambda: []
    line = run_cycle(app)
    assert "outcome=scope-offline" not in line
    assert app.state.latched()                         # the blip must not clear the latch
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]
    assert any("10.0.0.5 missed 1 poll" in r.getMessage() for r in caplog.records)
    app.scope_check = lambda: ["10.0.0.5"]
    run_cycle(app)
    assert [t for t, _ in app.notifier.sent] == ["Rain incoming"]   # and no "came online"


def test_never_seen_scope_is_offline_immediately(tmp_path):
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5"}, scope=lambda: [])
    assert "outcome=scope-offline" in run_cycle(app)


def test_debounce_survives_restart(tmp_path):
    app = scoped(tmp_path, lambda: ["10.0.0.5"])
    run_cycle(app)
    again = scoped(tmp_path, lambda: [])               # new process, same state dir
    assert "outcome=scope-offline" not in run_cycle(again)
    assert "outcome=scope-offline" in run_cycle(again)


def test_duplicate_host_entries_count_one_miss_per_poll(tmp_path):
    app = build(tmp_path, env={"SCOPE_HOSTS": "10.0.0.5,10.0.0.5:5555"}, scope=lambda: ["10.0.0.5"])
    run_cycle(app)
    app.scope_check = lambda: []
    assert "outcome=scope-offline" not in run_cycle(app)   # one missed poll, still in grace
    assert app.scope_misses["10.0.0.5"] == 1
    assert "outcome=scope-offline" in run_cycle(app)
