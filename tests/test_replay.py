import logging
from datetime import UTC, datetime, timedelta

import numpy as np

from astrorainprotect.config import load_config
from astrorainprotect.replay import DryRunNotifier, ReplaySource, load_frames, run_replay
from tests.conftest import make_frame

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)
BASE = {"LAT": "29.97", "LON": "-95.67", "NTFY_URL": "https://ntfy.example.net/rain"}


def save_sequence(d):
    for k in range(4):
        g = np.zeros((101, 101), dtype=np.float32)
        if k >= 2:
            g[60:64, 40:44] = 45.0                # storm appears at frame 2
        make_frame(g, product="reflectivity", valid_time=T0 + timedelta(minutes=2 * k)).save(
            d / f"reflectivity_{k}.npz")
        make_frame(np.zeros((101, 101)), product="preciprate",
                   valid_time=T0 + timedelta(minutes=2 * k)).save(d / f"preciprate_{k}.npz")


def test_load_frames_sorted(tmp_path):
    save_sequence(tmp_path)
    frames = load_frames(tmp_path)
    assert len(frames) == 8
    assert [f.valid_time for f in frames] == sorted(f.valid_time for f in frames)


def test_replay_source_time_travel(tmp_path):
    save_sequence(tmp_path)
    src = ReplaySource(load_frames(tmp_path))
    assert src.fetch_latest("reflectivity", T0 - timedelta(minutes=1)) is None
    f = src.fetch_latest("reflectivity", T0 + timedelta(minutes=3))
    assert f.valid_time == T0 + timedelta(minutes=2)
    src.fetch_latest("reflectivity", T0 + timedelta(minutes=7))
    assert [x.valid_time for x in src.frames("reflectivity")] == [
        T0 + timedelta(minutes=2), T0 + timedelta(minutes=6)]


def test_run_replay_reports_would_send(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    save_sequence(tmp_path)
    cfg = load_config({**BASE, "REPLAY_DIR": str(tmp_path), "STATE_DIR": str(tmp_path / "unused")})
    n = run_replay(cfg, tmp_path)
    assert n == 4
    would = [r.getMessage() for r in caplog.records if r.getMessage().startswith("WOULD SEND")]
    assert len(would) == 1 and "Rain incoming" in would[0]
    assert not (tmp_path / "unused").exists()     # replay never touches the real state dir


def test_dry_run_notifier(caplog):
    caplog.set_level(logging.INFO)
    assert DryRunNotifier().send("T", "M") is True
    assert "WOULD SEND T: M" in caplog.records[-1].getMessage()
