from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.detect import KM_PER_DEG
from astrorainprotect.motion import Motion, estimate, phase_shift
from tests.conftest import make_frame

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


def blob(cy, cx, n=101, value=40.0):
    g = np.zeros((n, n), dtype=np.float32)
    yy, xx = np.mgrid[:n, :n]
    g[(yy - cy) ** 2 + (xx - cx) ** 2 < 36] = value
    return g


def test_phase_shift_recovers_roll():
    a = blob(30, 40)
    b = np.roll(a, (7, -12), axis=(0, 1))
    dy, dx, peak = phase_shift(a, b)
    assert (dy, dx) == (7, -12)
    assert peak > 0.9


def test_phase_shift_empty_has_zero_peak():
    z = np.zeros((20, 20))
    assert phase_shift(z, z)[2] == 0.0


def test_estimate_needs_three_frames():
    f = make_frame(blob(30, 40), product="reflectivity", valid_time=T0)
    g = make_frame(blob(30, 40), product="reflectivity", valid_time=T0 + timedelta(minutes=2))
    assert estimate([f, g]) is None


def test_estimate_vector_units_and_sign():
    # blob moves 6 rows south and 8 columns east over 4 minutes (2 frames apart)
    frames = [make_frame(np.roll(blob(30, 40), (3 * k, 4 * k), axis=(0, 1)), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    m = estimate(frames)
    assert isinstance(m, Motion)
    assert m.frames_used == 3
    km_per_cell_ns = 0.01 * KM_PER_DEG
    km_per_cell_ew = 0.01 * KM_PER_DEG * np.cos(np.radians(29.97))
    # southward => negative v
    assert m.v_km_per_min == pytest.approx(-6 * km_per_cell_ns / 4, rel=0.01)
    # eastward => positive u
    assert m.u_km_per_min == pytest.approx(8 * km_per_cell_ew / 4, rel=0.01)
    assert m.confidence > 0.9


def test_estimate_rejects_low_confidence():
    rng = np.random.default_rng(1)
    frames = [make_frame(rng.random((101, 101)) * 50, product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


def test_estimate_rejects_empty_frames():
    frames = [make_frame(np.zeros((101, 101)), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


def test_estimate_rejects_identical_times():
    f = make_frame(blob(30, 40), product="reflectivity", valid_time=T0)
    assert estimate([f, f, f]) is None
