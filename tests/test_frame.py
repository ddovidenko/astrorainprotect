from datetime import UTC, datetime

import numpy as np

from astrorainprotect.frame import Frame
from tests.conftest import make_frame


def test_make_frame_geometry():
    f = make_frame(np.zeros((5, 7)))
    assert f.values.shape == (5, 7)
    assert f.lats[0] > f.lats[-1]            # descending, row 0 north
    assert f.lons[0] < f.lons[-1]            # ascending
    assert abs(f.lats[2] - 29.97) < 1e-9
    assert abs(f.lons[3] + 95.67) < 1e-9


def test_save_load_roundtrip(tmp_path):
    f = make_frame(np.arange(12, dtype=np.float32).reshape(3, 4), product="reflectivity",
                   valid_time=datetime(2026, 9, 23, 1, 10, 40, tzinfo=UTC), missing_fraction=0.25)
    path = tmp_path / "x.npz"
    f.save(path)
    g = Frame.load(path)
    assert g.product == "reflectivity"
    assert g.valid_time == f.valid_time
    assert g.valid_time.tzinfo is not None
    assert g.missing_fraction == 0.25
    np.testing.assert_array_equal(g.values, f.values)
    np.testing.assert_array_equal(g.lats, f.lats)
    np.testing.assert_array_equal(g.lons, f.lons)


def test_filename_for():
    f = make_frame(np.zeros((1, 1)), product="reflectivity",
                   valid_time=datetime(2026, 9, 23, 1, 10, 40, tzinfo=UTC))
    assert f.filename() == "reflectivity_20260923-011040.npz"
