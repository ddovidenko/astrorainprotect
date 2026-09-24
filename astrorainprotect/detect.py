"""Pure radar detection logic. No I/O."""

from __future__ import annotations

import numpy as np

KM_PER_DEG = 111.195  # mean earth radius * pi / 180

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")


def distance_bearing_grid(lats: np.ndarray, lons: np.ndarray, home_lat: float, home_lon: float):
    """Equirectangular distance (km) and bearing (deg, 0=N, 90=E) from home to every cell."""
    lat_g, lon_g = np.meshgrid(lats, lons, indexing="ij")
    dy = (lat_g - home_lat) * KM_PER_DEG
    dx = (lon_g - home_lon) * KM_PER_DEG * np.cos(np.radians(home_lat))
    dist = np.hypot(dx, dy)
    bearing = np.degrees(np.arctan2(dx, dy)) % 360.0
    return dist, bearing


def compass(bearing_deg: float) -> str:
    return _COMPASS[int(((bearing_deg + 22.5) % 360) // 45)]
