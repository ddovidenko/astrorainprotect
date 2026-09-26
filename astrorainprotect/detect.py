"""Pure radar detection logic. No I/O."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from astrorainprotect.frame import Frame

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


@dataclass(frozen=True)
class Detection:
    product: str
    raining_now: bool
    rate_at_house: float
    qualifying_cells: int
    nearby: bool
    nearest_km: float | None
    nearest_bearing_deg: float | None
    max_value: float

    def describe(self) -> str:
        if self.nearest_km is None or self.nearest_bearing_deg is None:
            return "no echo in range"
        return f"{self.nearest_km:.1f} km to the {compass(self.nearest_bearing_deg)}"


def detect(frame: Frame, home_lat: float, home_lon: float, *, now_radius_km: float,
           alert_radius_km: float, now_threshold: float, nearby_threshold: float,
           min_cells: int) -> Detection:
    dist, bearing = distance_bearing_grid(frame.lats, frame.lons, home_lat, home_lon)
    values = frame.values
    in_now = dist <= now_radius_km
    in_alert = dist <= alert_radius_km

    rate_at_house = float(values[in_now].max()) if in_now.any() else 0.0
    raining_now = in_now.any() and rate_at_house >= now_threshold

    qualifying = in_alert & (values >= nearby_threshold)
    n = int(qualifying.sum())
    max_value = float(values[in_alert].max()) if in_alert.any() else 0.0

    nearest_km = nearest_bearing = None
    if n:
        idx = np.argmin(np.where(qualifying, dist, np.inf))
        j, i = np.unravel_index(idx, dist.shape)
        nearest_km, nearest_bearing = float(dist[j, i]), float(bearing[j, i])

    return Detection(
        product=frame.product,
        raining_now=bool(raining_now),
        rate_at_house=rate_at_house,
        qualifying_cells=n,
        nearby=n >= min_cells,
        nearest_km=nearest_km,
        nearest_bearing_deg=nearest_bearing,
        max_value=max_value,
    )
