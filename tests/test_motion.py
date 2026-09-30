from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from astrorainprotect.detect import KM_PER_DEG
from astrorainprotect.motion import Motion, estimate, xcorr_shift
from tests.conftest import make_frame

T0 = datetime(2026, 9, 23, 20, 0, tzinfo=UTC)


def blob(cy, cx, n=101, value=40.0, r=6):
    g = np.zeros((n, n), dtype=np.float32)
    yy, xx = np.mgrid[:n, :n]
    g[(yy - cy) ** 2 + (xx - cx) ** 2 < r * r] = value
    return g


def refl(values, minute):
    return make_frame(values, product="reflectivity", valid_time=T0 + timedelta(minutes=minute))


def test_xcorr_shift_recovers_roll():
    a = (blob(30, 40) > 0).astype(np.float32)
    b = np.roll(a, (7, -12), axis=(0, 1))
    dy, dx, corr = xcorr_shift(a, b, max_dy=20, max_dx=20)
    assert (dy, dx) == (7, -12)
    assert corr > 0.9


def test_xcorr_shift_empty_has_zero_corr():
    z = np.zeros((20, 20), dtype=np.float32)
    assert xcorr_shift(z, z, max_dy=5, max_dx=5)[2] == 0.0


def test_xcorr_shift_only_searches_plausible_shifts():
    # the true displacement (40 columns) is outside the search window, so it is never reported
    a = (blob(30, 20) > 0).astype(np.float32)
    b = np.roll(a, (0, 40), axis=(0, 1))
    dy, dx, corr = xcorr_shift(a, b, max_dy=10, max_dx=10)
    assert abs(dx) <= 10 and abs(dy) <= 10
    assert corr < 0.1


def test_xcorr_shift_search_window_is_an_ellipse():
    # (9, 9) is inside the 10 x 10 box but outside the ellipse, so it is not found
    a = (blob(40, 40) > 0).astype(np.float32)
    b = np.roll(a, (9, 9), axis=(0, 1))
    dy, dx, corr = xcorr_shift(a, b, max_dy=10, max_dx=10)
    assert (dy / 10) ** 2 + (dx / 10) ** 2 <= 1.0


def test_xcorr_shift_does_not_wrap_around_the_box():
    # a cell at the west edge dissipates and an unrelated one sits at the east edge: on a
    # torus those are 12 columns apart, in the box they are 89 apart
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[40:60, 0:12] = 1.0
    b[40:60, 89:101] = 1.0
    dy, dx, corr = xcorr_shift(a, b, max_dy=20, max_dx=20)
    assert corr < 0.1


def test_xcorr_shift_does_not_wrap_north_to_south():
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[0:12, 40:60] = 1.0
    b[89:101, 40:60] = 1.0
    assert xcorr_shift(a, b, max_dy=20, max_dx=20)[2] < 0.1


def test_xcorr_shift_reports_correlation_at_the_shift_it_returns():
    # the echo splits and moves both ways: two tied peaks, and their middle matches nothing
    a = (blob(50, 50) > 0).astype(np.float32)
    b = np.maximum(np.roll(a, (0, 15), axis=(0, 1)), np.roll(a, (0, -15), axis=(0, 1)))
    dy, dx, corr = xcorr_shift(a, b, max_dy=20, max_dx=20)
    assert (dy, dx) == (0, 0)
    assert corr < 0.1


def test_xcorr_shift_tie_centre_outside_the_window_is_finite():
    a = np.zeros((40, 40), dtype=np.float32)
    a[20, 10] = 1.0
    b = np.zeros((40, 40), dtype=np.float32)
    b[20, 10 + 3] = 1.0
    b[23, 10] = 1.0                     # ties at (0, 3) and (3, 0); their centre (2, 2) rounds
    dy, dx, corr = xcorr_shift(a, b, max_dy=3, max_dx=3)     # outside the 3 x 3 ellipse
    assert np.isfinite(corr)


def test_estimate_vector_units_and_sign():
    # blob moves 6 rows south and 8 columns east over 10 minutes
    frames = [refl(np.roll(blob(30, 40), (3 * k, 4 * k), axis=(0, 1)), 5 * k) for k in range(3)]
    m, reason = estimate(frames)
    assert isinstance(m, Motion) and reason == ""
    assert m.baseline_min == 10
    km_per_cell_ns = 0.01 * KM_PER_DEG
    km_per_cell_ew = 0.01 * KM_PER_DEG * np.cos(np.radians(29.97))
    # southward => negative v
    assert m.v_km_per_min == pytest.approx(-6 * km_per_cell_ns / 10, rel=0.01)
    # eastward => positive u
    assert m.u_km_per_min == pytest.approx(8 * km_per_cell_ew / 10, rel=0.01)
    assert m.confidence > 0.9


def test_estimate_survives_growth_and_a_new_cell():
    """Issue #47: real convection grows and deforms while it moves; a rigid roll is not enough."""
    frames = []
    for k in range(6):                                   # 4-minute frames, 20 minutes in all
        g = blob(70 - 2 * k, 30 + k, r=5 + k)            # grows while moving 2 north, 1 east
        if k >= 3:                                       # a second cell forms in place and moves
            g = np.maximum(g, blob(40 - 2 * k, 60 + k, r=2 * (k - 2), value=30.0))
        frames.append(refl(g, 4 * k))
    m, reason = estimate(frames)
    assert m is not None, reason
    assert m.baseline_min == 20
    assert m.v_km_per_min == pytest.approx(10 * 0.01 * KM_PER_DEG / 20, rel=0.15)
    assert m.u_km_per_min == pytest.approx(
        5 * 0.01 * KM_PER_DEG * np.cos(np.radians(29.97)) / 20, abs=0.03)


@pytest.mark.parametrize("spacing", [2, 5])
def test_estimate_baseline_is_chosen_by_time_not_frame_count(spacing):
    # 30 minutes of history at either poll spacing: the frame 20 minutes back is the baseline
    frames = [refl(np.roll(blob(50, 20), (0, t // 2), axis=(0, 1)), t)
              for t in range(0, 31, spacing)]
    m, _ = estimate(frames)
    assert m is not None and m.baseline_min == 20


def test_estimate_needs_eight_minutes_of_history():
    frames = [refl(np.roll(blob(30, 40), (0, 3 * k), axis=(0, 1)), 2 * k) for k in range(3)]
    assert estimate(frames) == (None, "no baseline")


def test_estimate_single_frame_has_no_baseline():
    assert estimate([refl(blob(30, 40), 0)]) == (None, "no baseline")
    assert estimate([]) == (None, "no baseline")


def test_estimate_ignores_frames_from_before_a_gap():
    # the scope gate was closed for an hour: the old frame must not become the baseline
    frames = [refl(blob(30, 40), 0), refl(np.roll(blob(30, 40), (0, 5), axis=(0, 1)), 60)]
    assert estimate(frames) == (None, "no baseline")


def test_estimate_rejects_sparse_echo():
    # 13 cells is too little signal; on the recorded storm such masks gave wild shifts
    frames = [refl(np.roll(blob(30, 40, r=2), (0, 4 * k), axis=(0, 1)), 5 * k) for k in range(3)]
    assert estimate(frames) == (None, "few cells")


def rect(cells, col):
    g = np.zeros((101, 101), dtype=np.float32)
    g.flat[[r * 101 + col + c for r in range(40, 46) for c in range(5)][:cells]] = 40.0
    return g


def test_estimate_accepts_thirty_cells_and_rejects_twenty_nine():
    assert estimate([refl(rect(30, 20), 0), refl(rect(30, 26), 10)])[0] is not None
    assert estimate([refl(rect(29, 20), 0), refl(rect(29, 26), 10)]) == (None, "few cells")


@pytest.mark.parametrize("sparse", [0, 1])
def test_estimate_needs_enough_echo_in_both_frames(sparse):
    grids = [blob(30, 40), np.roll(blob(30, 40), (0, 6), axis=(0, 1))]
    grids[sparse] = np.roll(blob(30, 40, r=2), (0, 6 * sparse), axis=(0, 1))
    assert estimate([refl(grids[0], 0), refl(grids[1], 10)]) == (None, "few cells")


def test_estimate_skips_a_baseline_of_another_shape():
    frames = [refl(blob(30, 40, n=81), 0), refl(np.roll(blob(30, 40), (0, 6), axis=(0, 1)), 10)]
    assert estimate(frames) == (None, "no baseline")


def test_estimate_ignores_echo_appearing_across_the_box_edge():
    # nothing moves: one cell fades at the west edge, another shows up at the east edge
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[40:60, 0:12] = 40.0
    b[40:60, 89:101] = 40.0
    m, reason = estimate([refl(a, 0), refl(b, 10)])
    assert m is None and reason.startswith("corr")


def test_estimate_rejects_empty_frames():
    frames = [refl(np.zeros((101, 101)), 5 * k) for k in range(3)]
    assert estimate(frames) == (None, "few cells")


def test_estimate_rejects_low_correlation_and_says_so():
    rng = np.random.default_rng(1)
    frames = [refl(rng.random((101, 101)) * 50, 5 * k) for k in range(3)]
    m, reason = estimate(frames)
    assert m is None
    assert reason.startswith("corr 0.")


def test_estimate_reports_the_correlation_it_rejected():
    # 20 of 100 cells line up with the earlier block; the rest reappear far out of reach
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[10:20, 10:20] = 40.0
    b[10:20, 14:16] = 40.0
    b[80:90, 60:68] = 40.0
    assert estimate([refl(a, 0), refl(b, 10)]) == (None, "corr 0.19")


def test_estimate_rejects_a_shift_at_the_edge_of_the_search():
    # unrelated thick echoes at opposite edges: the best match is as far as the search reaches
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[40:61, 0:30] = 40.0
    b[40:61, 71:101] = 40.0
    assert estimate([refl(a, 0), refl(b, 30)]) == (None, "shift at search limit")


def test_estimate_rejects_a_shift_that_matches_no_better_than_standing_still():
    # the echo shrinks inside its old outline; on the recorded storm such decay read as motion
    a = np.zeros((101, 101), dtype=np.float32)
    b = np.zeros((101, 101), dtype=np.float32)
    a[30:71, 30:71] = 40.0
    b[30:71, 33:61] = 40.0
    assert estimate([refl(a, 0), refl(b, 10)]) == (None, "no clear shift")


def gappy(values, minute, missing):
    return make_frame(values, product="reflectivity", valid_time=T0 + timedelta(minutes=minute),
                      missing_fraction=missing)


def test_estimate_skips_a_baseline_with_radar_gaps():
    # part of the box had no coverage in the older frame: its echo is cut off, not moved
    moved = np.roll(blob(30, 40), (0, 6), axis=(0, 1))
    assert estimate([gappy(blob(30, 40), 0, 0.28), refl(moved, 10)]) == (None, "no baseline")


def test_estimate_rejects_a_newest_frame_with_radar_gaps():
    moved = np.roll(blob(30, 40), (0, 6), axis=(0, 1))
    assert estimate([refl(blob(30, 40), 0), gappy(moved, 10, 0.28)]) == (None, "radar gaps")


def test_estimate_rejects_implausible_speed():
    # 45 cells in 10 minutes is about 260 km/h: not a storm, never reported as motion
    frames = [refl(blob(30, 20), 0), refl(blob(30, 65), 10)]
    m, reason = estimate(frames)
    assert m is None and reason.startswith("corr")


def test_estimate_rejects_zero_shift():
    frames = [refl(blob(30, 40), 5 * k) for k in range(3)]
    assert estimate(frames) == (None, "small shift")


def test_estimate_rejects_sub_resolution_shift():
    # total displacement of 1 cell across the whole window is below MIN_SHIFT_CELLS
    frames = [refl(np.roll(blob(30, 40), (0, min(k, 1)), axis=(0, 1)), 5 * k) for k in range(3)]
    assert estimate(frames) == (None, "small shift")


from astrorainprotect.detect import Detection  # noqa: E402
from astrorainprotect.motion import Approach, project  # noqa: E402


def det(km, bearing):
    return Detection("reflectivity", False, 0.0, 9, True, km, bearing, 40.0)


def test_project_toward_house():
    # echo 12 km to the SW (bearing 225), moving NE at 1 km/min
    m = Motion(u_km_per_min=np.sin(np.radians(45)), v_km_per_min=np.cos(np.radians(45)),
               confidence=1, baseline_min=10)
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert isinstance(a, Approach)
    assert a.will_hit
    assert a.eta_min == pytest.approx(7.0, abs=0.1)
    assert a.closest_km == pytest.approx(0.0, abs=1e-6)


def test_project_moving_away():
    m = Motion(u_km_per_min=-0.7, v_km_per_min=-0.7, confidence=1, baseline_min=10)   # SW-bound
    a = project(det(12.0, 225.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.eta_min is None
    assert a.closest_km == pytest.approx(12.0)


def test_project_passes_wide():
    # echo 10 km due south, moving due east: closest approach is 10 km, outside 5 km
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(det(10.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(10.0)


def test_project_too_slow_for_lookahead():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.1, confidence=1, baseline_min=10)   # 6 km/h north
    a = project(det(15.0, 180.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit


def test_project_already_inside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(det(2.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit and a.eta_min == 0.0


def test_project_stationary_outside():
    m = Motion(u_km_per_min=0.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(det(8.0, 90.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.closest_km == pytest.approx(8.0)


def test_project_uses_every_cell_not_just_nearest():
    """Issue #11: a cell farther away can be the one that hits; the nearest may pass wide."""
    # nearest cell 8 km due south, another 12 km due west; storm moving east at 1 km/min
    offsets = np.array([[0.0, -8.0], [-12.0, 0.0]])
    d = Detection("reflectivity", False, 0.0, 2, True, 8.0, 180.0, 40.0, offsets_km=offsets)
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(d, m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit
    assert a.eta_min == pytest.approx(7.0, abs=0.1)        # west cell enters the 5 km circle
    assert a.closest_km == pytest.approx(0.0, abs=1e-6)


def test_project_all_cells_miss_reports_min_closest():
    offsets = np.array([[0.0, -8.0], [0.0, 12.0]])           # south and north, moving east
    d = Detection("reflectivity", False, 0.0, 2, True, 8.0, 180.0, 40.0, offsets_km=offsets)
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(d, m, hit_radius_km=5.0, lookahead_min=60)
    assert not a.will_hit and a.eta_min is None
    assert a.closest_km == pytest.approx(8.0)


def test_project_eta_is_earliest_entering_cell():
    offsets = np.array([[-20.0, 0.0], [-10.0, 0.0]])
    d = Detection("reflectivity", False, 0.0, 2, True, 10.0, 270.0, 40.0, offsets_km=offsets)
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(d, m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit and a.eta_min == pytest.approx(5.0, abs=0.1)


def test_project_without_offsets_falls_back_to_nearest():
    m = Motion(u_km_per_min=1.0, v_km_per_min=0.0, confidence=1, baseline_min=10)
    a = project(det(10.0, 270.0), m, hit_radius_km=5.0, lookahead_min=60)
    assert a.will_hit and a.eta_min == pytest.approx(5.0, abs=0.1)
