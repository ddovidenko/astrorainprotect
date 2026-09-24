"""Storm motion from consecutive frames (FFT phase correlation) and approach projection. Pure."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrorainprotect.detect import KM_PER_DEG, Detection
from astrorainprotect.frame import Frame

MIN_CONFIDENCE = 0.3
MIN_SHIFT_CELLS = 2   # below this, a whole-cell phase-correlation shift is not trustworthy


@dataclass(frozen=True)
class Motion:
    """Storm motion vector, from estimate(). Only returned when the shift is >= MIN_SHIFT_CELLS."""

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
    """Estimate storm motion between the first and last frame, or None when motion is not known.

    Returns None when there are too few frames, the frames don't line up, the phase-correlation
    peak is too weak, or the whole-cell shift is smaller than MIN_SHIFT_CELLS: a shift that small
    (including exactly 0) is not trustworthy motion evidence, even if the peak confidence is high,
    since phase_shift only resolves whole-cell displacement.
    """
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
    if math.hypot(dy, dx) < MIN_SHIFT_CELLS:
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


def project(detection: Detection, motion: Motion, *, hit_radius_km: float,
            lookahead_min: float) -> Approach:
    if detection.nearest_km is None or detection.nearest_bearing_deg is None:
        return Approach(False, None, float("inf"))
    b = np.radians(detection.nearest_bearing_deg)
    px, py = detection.nearest_km * np.sin(b), detection.nearest_km * np.cos(b)   # east, north
    vx, vy = motion.u_km_per_min, motion.v_km_per_min
    r0 = float(np.hypot(px, py))
    if r0 <= hit_radius_km:
        return Approach(True, 0.0, r0)
    v2 = vx * vx + vy * vy
    if v2 < 1e-9:
        return Approach(False, None, r0)
    t_closest = float(np.clip(-(px * vx + py * vy) / v2, 0.0, lookahead_min))
    closest = float(np.hypot(px + vx * t_closest, py + vy * t_closest))
    if closest > hit_radius_km:
        return Approach(False, None, closest)
    # smallest t >= 0 with |p + v t| == hit_radius: solve v2 t^2 + 2(p.v) t + (r0^2 - R^2) = 0
    bq = 2 * (px * vx + py * vy)
    cq = r0 * r0 - hit_radius_km * hit_radius_km
    disc = bq * bq - 4 * v2 * cq
    t_enter = (-bq - np.sqrt(max(disc, 0.0))) / (2 * v2)
    if t_enter < 0 or t_enter > lookahead_min:
        return Approach(False, None, closest)
    return Approach(True, float(t_enter), closest)
