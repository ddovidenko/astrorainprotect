import logging
from datetime import UTC, datetime, timedelta

import numpy as np

from astrorainprotect.recorder import Recorder
from astrorainprotect.replay import load_frames
from tests.conftest import make_frame

T0 = datetime(2026, 10, 2, 3, 0, tzinfo=UTC)


def frames(n, product="reflectivity", start=T0):
    return [make_frame(np.zeros((5, 5)), product=product,
                       valid_time=start + timedelta(minutes=2 * k)) for k in range(n)]


def test_send_starts_a_recording_with_the_cached_history(tmp_path):
    rec = Recorder(tmp_path / "recordings")
    rec.update(T0 + timedelta(minutes=10), "send", frames(6) + frames(6, "preciprate"))
    folders = list((tmp_path / "recordings").iterdir())
    assert [f.name for f in folders] == ["20261002-0310"]
    assert len(list(folders[0].glob("*.npz"))) == 12
    assert len(load_frames(folders[0])) == 12          # replayable as it is


def test_each_cycle_adds_only_new_frames(tmp_path):
    rec = Recorder(tmp_path)
    hist = frames(3)
    rec.update(T0, "send", hist)
    hist = hist[1:] + frames(1, start=T0 + timedelta(minutes=6))
    rec.update(T0 + timedelta(minutes=5), "skip", hist)
    assert len(list(rec.path.glob("*.npz"))) == 4


def test_nothing_is_recorded_without_an_alert(tmp_path):
    rec = Recorder(tmp_path)
    rec.update(T0, "none", frames(3))
    rec.update(T0, "skip", frames(3))
    assert not rec.active and list(tmp_path.iterdir()) == []


def test_recording_closes_thirty_minutes_after_rearm(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    rec = Recorder(tmp_path)
    rec.update(T0, "send", frames(2))
    rec.update(T0 + timedelta(minutes=60), "re-armed", frames(2, start=T0 + timedelta(minutes=58)))
    assert rec.active
    rec.update(T0 + timedelta(minutes=85), "none", frames(2, start=T0 + timedelta(minutes=83)))
    assert rec.active                                  # the recession tail is still being saved
    rec.update(T0 + timedelta(minutes=90), "none", frames(2, start=T0 + timedelta(minutes=88)))
    assert not rec.active
    assert any("recording closed" in r.getMessage() and "8 frames" in r.getMessage()
               for r in caplog.records)


def test_new_alert_during_the_tail_keeps_the_same_recording(tmp_path):
    rec = Recorder(tmp_path)
    rec.update(T0, "send", frames(1))
    rec.update(T0 + timedelta(minutes=10), "re-armed", frames(1, start=T0 + timedelta(minutes=10)))
    first = rec.path
    rec.update(T0 + timedelta(minutes=20), "send", frames(1, start=T0 + timedelta(minutes=20)))
    rec.update(T0 + timedelta(minutes=55), "skip", frames(1, start=T0 + timedelta(minutes=55)))
    assert rec.active and rec.path == first
    assert len(list(tmp_path.iterdir())) == 1


def test_close_now_on_request(tmp_path):
    rec = Recorder(tmp_path)
    rec.update(T0, "send", frames(1))
    rec.close("scope gate closed")
    assert not rec.active
    rec.update(T0 + timedelta(minutes=5), "none", frames(1))
    assert not rec.active


def test_only_the_newest_recordings_are_kept(tmp_path):
    rec = Recorder(tmp_path, keep=2)
    for k in range(3):
        t = T0 + timedelta(hours=k)
        rec.update(t, "send", frames(1, start=t))
        rec.close("test")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20261002-0400", "20261002-0500"]


def test_write_failure_is_logged_not_raised(tmp_path, caplog, monkeypatch):
    caplog.set_level(logging.ERROR)
    rec = Recorder(tmp_path)
    monkeypatch.setattr("astrorainprotect.frame.Frame.save", lambda self, p: 1 / 0)
    rec.update(T0, "send", frames(2))
    assert any("recording" in r.getMessage() for r in caplog.records)
    rec.update(T0 + timedelta(minutes=5), "skip", frames(2))   # keeps going without raising
