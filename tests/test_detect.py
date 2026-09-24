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
