"""Storm motion from consecutive frames (FFT phase correlation) and approach projection. Pure."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrorainprotect.detect import KM_PER_DEG
from astrorainprotect.frame import Frame

MIN_CONFIDENCE = 0.3


@dataclass(frozen=True)
class Motion:
    u_km_per_min: float   # eastward
    v_km_per_min: float   # northward
    confidence: float     # phase-correlation peak, 0..1
    frames_used: int


@dataclass(frozen=True)
class Approach:
    will_hit: bool
    eta_min: float | None
    closest_km: float


def phase_shift(a: np.ndarray, b: np.ndarray) -> tuple[int, int, float]:
    """(dy, dx, peak) such that b ≈ np.roll(a, (dy, dx), axis=(0, 1))."""
    fa, fb = np.fft.fft2(a), np.fft.fft2(b)
    cross = fb * np.conj(fa)
    mag = np.abs(cross)
    if not mag.any():
        return 0, 0, 0.0
    mag[mag == 0] = 1.0
    r = np.fft.ifft2(cross / mag).real
    dy, dx = np.unravel_index(int(np.argmax(r)), r.shape)
    if dy > a.shape[0] // 2:
        dy -= a.shape[0]
    if dx > a.shape[1] // 2:
        dx -= a.shape[1]
    return int(dy), int(dx), float(r.max())


def estimate(frames: Sequence[Frame], *, min_confidence: float = MIN_CONFIDENCE) -> Motion | None:
    if len(frames) < 3:
        return None
    first, last = frames[0], frames[-1]
    dt_min = (last.valid_time - first.valid_time).total_seconds() / 60.0
    if dt_min <= 0 or first.values.shape != last.values.shape:
        return None
    if not first.values.any() or not last.values.any():
        return None
    dy, dx, peak = phase_shift(first.values, last.values)
    if peak < min_confidence:
        return None
    lat = float(np.mean(last.lats))
    dlat = abs(float(last.lats[0] - last.lats[1])) if len(last.lats) > 1 else 0.01
    dlon = abs(float(last.lons[1] - last.lons[0])) if len(last.lons) > 1 else 0.01
    km_ns = dlat * KM_PER_DEG
    km_ew = dlon * KM_PER_DEG * np.cos(np.radians(lat))
    return Motion(
        u_km_per_min=dx * km_ew / dt_min,
        v_km_per_min=-dy * km_ns / dt_min,   # rows increase southward
        confidence=peak,
        frames_used=len(frames),
    )
