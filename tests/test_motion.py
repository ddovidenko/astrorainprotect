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


def test_estimate_rejects_zero_shift():
    # identical content at distinct valid times: peak is 1.0, but there is no shift at all
    frames = [make_frame(blob(30, 40), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


def test_estimate_rejects_sub_resolution_shift():
    # total displacement of 1 cell across the whole window is below MIN_SHIFT_CELLS
    frames = [make_frame(np.roll(blob(30, 40), (0, min(k, 1)), axis=(0, 1)), product="reflectivity",
                         valid_time=T0 + timedelta(minutes=2 * k)) for k in range(3)]
    assert estimate(frames) is None


from astrorainprotect.detect import Detection  # noqa: E402
from astrorainprotect.motion import Approach, project  # noqa: E402


def det(km, bearing):
    return Detection("reflectivity", False, 0.0, 9, True, km, bearing, 40.0)


def test_project_toward_house():
    # echo 12 km to the SW (bearing 225), moving NE at 1 km/min
    m = Motion(u_km_per_min=np.sin(np.radians(45)), v_km_per_min=np.cos(np.radians(45)),
               confidence=1, frames_used=3)
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert isinstance(a, Approach)
    assert a.will_hit
    assert a.eta_min == pytest.approx(7.0, abs=0.1)
    assert a.closest_km == pytest.approx(0.0, abs=1e-6)


def test_project_moving_away():
    m = Motion(u_km_per_min=-0.7, v_km_per_min=-0.7, confidence=1, frames_used=3)   # SW-bound
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.eta_min is None
    assert a.closest_km == pytest.approx(12.0)


def test_project_passes_wide():
    # echo 10 km due south, moving due east: closest approach is 10 km, outside 5 km
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(10.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(10.0)


def test_project_too_slow_for_lookahead():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.1, confidence=1, frames_used=3)   # 6 km/h north
    a = project(det(15.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit


def test_project_already_inside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(2.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit and a.eta_min == 0.0


def test_project_stationary_outside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, frames_used=3)
    a = project(det(8.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(8.0)
