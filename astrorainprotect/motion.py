"""Storm motion from two frames (cross-correlated echo masks) and approach projection. Pure."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from astrorainprotect.detect import KM_PER_DEG, Detection
from astrorainprotect.frame import Frame

MIN_CONFIDENCE = 0.3         # correlation coefficient; the 2026-09-29 storm gave 0.33..0.79 (#47)
MIN_SHIFT_CELLS = 2          # below this, a whole-cell shift is not trustworthy
MASK_DBZ = 20.0              # cells at or above this make up the echo mask that is correlated
MIN_MASK_CELLS = 30          # sparser masks gave wild shifts with a confident-looking peak
BASELINE_MIN = 20.0          # preferred age of the frame the newest one is compared with
BASELINE_RANGE_MIN = (8.0, 30.0)
MAX_SPEED_KM_PER_MIN = 2.0   # 120 km/h; faster shifts are not searched


@dataclass(frozen=True)
class Motion:
    """Storm motion vector, from estimate(). Only returned when the shift is >= MIN_SHIFT_CELLS."""

    u_km_per_min: float   # eastward
    v_km_per_min: float   # northward
    confidence: float     # correlation coefficient of the two echo masks at the shift
    baseline_min: float   # minutes between the two frames compared


@dataclass(frozen=True)
class Approach:
    will_hit: bool
    eta_min: float | None
    closest_km: float


def xcorr_shift(a: np.ndarray, b: np.ndarray, *, max_dy: float,
                max_dx: float) -> tuple[int, int, float]:
    """(dy, dx, corr) such that b ≈ np.roll(a, (dy, dx), axis=(0, 1)), for 0/1 echo masks.

    Normalised cross-correlation, searched only over shifts inside the ellipse max_dy, max_dx.
    Unlike phase correlation it does not whiten the spectrum, so echoes that grow, decay and
    deform while they move still correlate (#47).
    """
    a0, b0 = a.astype(np.float64), b.astype(np.float64)
    a0, b0 = a0 - a0.mean(), b0 - b0.mean()
    norm = math.sqrt(float((a0 * a0).sum()) * float((b0 * b0).sum()))
    if norm == 0.0:
        return 0, 0, 0.0
    r = np.fft.ifft2(np.fft.fft2(b0) * np.conj(np.fft.fft2(a0))).real
    n, m = a.shape
    sy = np.fft.fftfreq(n, 1.0 / n)[:, None]     # signed shift of each row/column of r
    sx = np.fft.fftfreq(m, 1.0 / m)[None, :]
    allowed = (sy / max(max_dy, 1.0)) ** 2 + (sx / max(max_dx, 1.0)) ** 2 <= 1.0
    r = np.where(allowed, r, -np.inf)
    # Mask overlaps are whole cell counts, so neighbouring shifts often tie (a small echo that
    # fits anywhere inside the larger one it grew into). Take the middle of the tied shifts,
    # not whichever argmax meets first, and report the correlation there.
    tj, ti = np.nonzero(r >= r.max() - 0.5)
    dy, dx = round(float(sy[tj, 0].mean())), round(float(sx[0, ti].mean()))
    return dy, dx, float(r[dy % n, dx % m] / norm)


def _baseline(frames: Sequence[Frame]) -> tuple[Frame, float] | None:
    """The earlier frame closest to BASELINE_MIN before the newest, and its age in minutes.

    Chosen by time, not position: the cache holds one frame per poll, so the same count spans
    8 minutes at the product cadence and 20 at a 5-minute poll. The upper bound keeps a frame
    from before a gap (scope gate closed, outage) from being compared.
    """
    last = frames[-1]
    lo, hi = BASELINE_RANGE_MIN
    best: tuple[Frame, float] | None = None
    for f in frames[:-1]:
        dt = (last.valid_time - f.valid_time).total_seconds() / 60.0
        if not lo <= dt <= hi or f.values.shape != last.values.shape:
            continue
        if best is None or abs(dt - BASELINE_MIN) < abs(best[1] - BASELINE_MIN):
            best = (f, dt)
    return best


def estimate(frames: Sequence[Frame], *,
             min_confidence: float = MIN_CONFIDENCE) -> tuple[Motion | None, str]:
    """Estimate storm motion from reflectivity frames: (motion, "") or (None, reason).

    The reason says why motion is unknown: "no baseline" (no frame 8 to 30 minutes older than
    the newest), "few cells" (too little echo in either frame), "corr 0.NN" (the echo masks do
    not match at any plausible shift) or "small shift" (under MIN_SHIFT_CELLS, including none:
    only whole-cell displacement is resolved).
    """
    base = _baseline(frames) if frames else None
    if base is None:
        return None, "no baseline"
    first, dt_min = base
    last = frames[-1]
    a = (first.values >= MASK_DBZ).astype(np.float32)
    b = (last.values >= MASK_DBZ).astype(np.float32)
    if min(a.sum(), b.sum()) < MIN_MASK_CELLS:
        return None, "few cells"
    lat = float(np.mean(last.lats))
    dlat = abs(float(last.lats[0] - last.lats[1])) if len(last.lats) > 1 else 0.01
    dlon = abs(float(last.lons[1] - last.lons[0])) if len(last.lons) > 1 else 0.01
    km_ns = dlat * KM_PER_DEG
    km_ew = dlon * KM_PER_DEG * float(np.cos(np.radians(lat)))
    reach_km = MAX_SPEED_KM_PER_MIN * dt_min
    dy, dx, corr = xcorr_shift(a, b, max_dy=reach_km / km_ns, max_dx=reach_km / km_ew)
    if corr < min_confidence:
        return None, f"corr {corr:.2f}"
    if math.hypot(dy, dx) < MIN_SHIFT_CELLS:
        return None, "small shift"
    return Motion(
        u_km_per_min=dx * km_ew / dt_min,
        v_km_per_min=-dy * km_ns / dt_min,   # rows increase southward
        confidence=corr,
        baseline_min=dt_min,
    ), ""


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
