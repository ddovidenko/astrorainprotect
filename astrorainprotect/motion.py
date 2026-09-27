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
    """Project every qualifying cell along the motion vector (#11).

    will_hit when any cell's path enters the hit circle within lookahead_min; eta_min is the
    earliest entry; closest_km is the smallest closest approach over all cells. Falls back to
    the nearest cell alone when the detection carries no per-cell offsets.
    """
    if detection.offsets_km is not None and len(detection.offsets_km):
        p = np.asarray(detection.offsets_km, dtype=np.float64).reshape(-1, 2)
    elif detection.nearest_km is not None and detection.nearest_bearing_deg is not None:
        b = np.radians(detection.nearest_bearing_deg)
        p = np.array([[detection.nearest_km * np.sin(b), detection.nearest_km * np.cos(b)]])
    else:
        return Approach(False, None, float("inf"))
    px, py = p[:, 0], p[:, 1]                                          # east, north
    vx, vy = motion.u_km_per_min, motion.v_km_per_min
    r0 = np.hypot(px, py)
    if (r0 <= hit_radius_km).any():
        return Approach(True, 0.0, float(r0.min()))
    v2 = vx * vx + vy * vy
    if v2 < 1e-9:
        return Approach(False, None, float(r0.min()))
    pv = px * vx + py * vy
    t_closest = np.clip(-pv / v2, 0.0, lookahead_min)
    closest = np.hypot(px + vx * t_closest, py + vy * t_closest)
    closest_min = float(closest.min())
    # smallest t >= 0 with |p + v t| == hit_radius: solve v2 t^2 + 2(p.v) t + (r0^2 - R^2) = 0
    bq = 2 * pv
    cq = r0 * r0 - hit_radius_km * hit_radius_km
    disc = bq * bq - 4 * v2 * cq
    t_enter = (-bq - np.sqrt(np.maximum(disc, 0.0))) / (2 * v2)
    entering = (closest <= hit_radius_km) & (t_enter >= 0) & (t_enter <= lookahead_min)
    if not entering.any():
        return Approach(False, None, closest_min)
    return Approach(True, float(t_enter[entering].min()), closest_min)
