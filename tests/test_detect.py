import numpy as np
import pytest

from astrorainprotect.detect import compass, distance_bearing_grid

HOME = (29.97, -95.67)


def test_distance_zero_at_home():
    lats = np.array([29.98, 29.97, 29.96])
    lons = np.array([-95.68, -95.67, -95.66])
    dist, _ = distance_bearing_grid(lats, lons, *HOME)
    assert dist.shape == (3, 3)
    assert dist[1, 1] == pytest.approx(0.0, abs=1e-6)


def test_distance_one_degree_north():
    lats = np.array([30.97])
    lons = np.array([-95.67])
    dist, bearing = distance_bearing_grid(lats, lons, *HOME)
    assert dist[0, 0] == pytest.approx(111.2, abs=0.5)
    assert bearing[0, 0] == pytest.approx(0.0, abs=1e-6)


def test_distance_east_uses_cos_lat():
    lats = np.array([29.97])
    lons = np.array([-94.67])
    dist, bearing = distance_bearing_grid(lats, lons, *HOME)
    assert dist[0, 0] == pytest.approx(111.2 * np.cos(np.radians(29.97)), abs=0.5)
    assert bearing[0, 0] == pytest.approx(90.0, abs=1e-6)


@pytest.mark.parametrize("dlat,dlon,expected", [
    (0.1, 0.0, 0.0), (0.0, 0.1, 90.0), (-0.1, 0.0, 180.0), (0.0, -0.1, 270.0),
])
def test_bearing_quadrants(dlat, dlon, expected):
    lats = np.array([HOME[0] + dlat])
    lons = np.array([HOME[1] + dlon])
    _, bearing = distance_bearing_grid(lats, lons, *HOME)
    assert bearing[0, 0] == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("deg,name", [
    (0, "N"), (22, "N"), (45, "NE"), (90, "E"), (135, "SE"), (180, "S"),
    (225, "SW"), (270, "W"), (315, "NW"), (359, "N"),
])
def test_compass(deg, name):
    assert compass(deg) == name


from astrorainprotect.detect import Detection, detect  # noqa: E402
from tests.conftest import make_frame  # noqa: E402

KW = dict(now_radius_km=1.0, alert_radius_km=20.0, now_threshold=0.05,
          nearby_threshold=0.2, min_cells=3)


def grid(n=101):
    return np.zeros((n, n), dtype=np.float32)


def test_empty_grid():
    d = detect(make_frame(grid()), *HOME, **KW)
    assert isinstance(d, Detection)
    assert d.product == "preciprate"
    assert not d.raining_now and not d.nearby
    assert d.qualifying_cells == 0
    assert d.nearest_km is None and d.nearest_bearing_deg is None
    assert d.max_value == 0.0
    assert d.rate_at_house == 0.0


def test_single_pixel_below_min_cells():
    g = grid()
    g[40, 55] = 5.0                  # one loud pixel ~12 km NE
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 1
    assert not d.nearby
    assert d.nearest_km == pytest.approx(
        np.hypot(10 * 1.112, 5 * 1.112 * np.cos(np.radians(29.97))), rel=0.02
    )
    assert d.max_value == 5.0


def test_exactly_min_cells_is_nearby():
    g = grid()
    g[40, 55] = g[40, 56] = g[41, 55] = 0.2   # threshold is inclusive
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 3
    assert d.nearby


def test_just_below_threshold_not_counted():
    g = grid()
    g[40, 55] = g[40, 56] = g[41, 55] = 0.19
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 0


def test_cells_outside_radius_ignored():
    g = grid()
    g[0:3, 50] = 5.0                               # 50 rows north ≈ 55 km
    d = detect(make_frame(g), *HOME, **KW)
    assert d.qualifying_cells == 0
    # max is inside the alert radius only
    assert d.max_value == 0.0


def test_raining_now_uses_now_radius():
    g = grid()
    g[50, 50] = 0.05                   # at the house, exactly at threshold
    d = detect(make_frame(g), *HOME, **KW)
    assert d.raining_now
    assert d.rate_at_house == pytest.approx(0.05)
    g2 = grid()
    g2[50, 53] = 5.0                  # 3 cells east ≈ 2.9 km, outside 1 km
    d2 = detect(make_frame(g2), *HOME, **KW)
    assert not d2.raining_now
    assert d2.rate_at_house == 0.0


def test_nearest_and_bearing_southwest():
    g = grid()
    g[60:63, 40:43] = 1.0              # SW of the house
    d = detect(make_frame(g), *HOME, **KW)
    assert d.nearby
    assert 200 < d.nearest_bearing_deg < 250
    assert d.describe().endswith("to the SW")
    assert "km" in d.describe()


def test_reflectivity_thresholds():
    g = grid()
    g[40:43, 50:53] = 35.0           # ~10 km north
    d = detect(
        make_frame(g, product="reflectivity"),
        *HOME,
        now_radius_km=1.0,
        alert_radius_km=20.0,
        now_threshold=0.0,
        nearby_threshold=30.0,
        min_cells=3,
    )
    assert d.product == "reflectivity"
    assert d.nearby and d.qualifying_cells == 9
