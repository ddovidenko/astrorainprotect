from datetime import UTC, datetime

import numpy as np
import pytest

from astrorainprotect.frame import Frame

HOME = (29.97, -95.67)


def make_frame(values, *, product="preciprate", center=HOME, step=0.01,
               valid_time=datetime(2026, 9, 23, 20, 0, tzinfo=UTC), missing_fraction=0.0):
    """Build a Frame whose grid is centred on `center` with `step` degree spacing."""
    values = np.asarray(values, dtype=np.float32)
    nj, ni = values.shape
    lat0, lon0 = center
    lats = lat0 + step * (nj // 2 - np.arange(nj))   # row 0 is north
    lons = lon0 + step * (np.arange(ni) - ni // 2)
    return Frame(product=product, valid_time=valid_time, lats=lats, lons=lons,
                 values=values, missing_fraction=missing_fraction)


@pytest.fixture
def frame_factory():
    return make_frame
