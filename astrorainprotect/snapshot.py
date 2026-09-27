"""Radar snapshot PNG for ntfy attachments (issue #16). Pure: numpy + zlib, no image library.

The picture is the subset box around the house: reflectivity (or PrecipRate) on a dark
background, a crosshair on the house, a ring at the alert radius, a thinner ring at the hit
radius used by the direction filter, and the motion vector drawn from the strongest echo when
motion is known. No text: the notification body carries the numbers.
"""

from __future__ import annotations

import struct
import zlib

import numpy as np

from astrorainprotect.detect import KM_PER_DEG
from astrorainprotect.frame import Frame
from astrorainprotect.motion import Motion

SCALE = 4                     # output pixels per grid cell (101 cells -> 404 px)
BACKGROUND = (24, 26, 32)
HOUSE = (255, 255, 255)
ALERT_RING = (200, 200, 200)
HIT_RING = (120, 120, 120)
VECTOR = (255, 255, 0)
VECTOR_MINUTES = 30.0         # length of the drawn motion vector

# (threshold, colour): NWS-style reflectivity ramp, lowest first.
_DBZ_RAMP = [
    (5, (4, 233, 231)), (15, (1, 159, 244)), (20, (3, 0, 244)), (25, (2, 253, 2)),
    (30, (1, 197, 1)), (35, (0, 142, 0)), (40, (253, 248, 2)), (45, (229, 188, 0)),
    (50, (253, 149, 0)), (55, (253, 0, 0)), (60, (212, 0, 0)), (65, (255, 0, 255)),
    (70, (153, 85, 201)),
]
# Rain rate in mm/h mapped onto the same colours.
_RATE_RAMP = [
    (0.1, (4, 233, 231)), (0.5, (1, 159, 244)), (1, (3, 0, 244)), (2, (2, 253, 2)),
    (4, (1, 197, 1)), (6, (0, 142, 0)), (10, (253, 248, 2)), (15, (229, 188, 0)),
    (20, (253, 149, 0)), (30, (253, 0, 0)), (50, (212, 0, 0)), (75, (255, 0, 255)),
    (100, (153, 85, 201)),
]


def encode_png(rgb: np.ndarray) -> bytes:
    """8-bit RGB PNG, one filter-0 byte per row, zlib compressed."""
    rgb = np.ascontiguousarray(rgb, dtype=np.uint8)
    h, w, _ = rgb.shape
    rows = np.concatenate([np.zeros((h, 1), np.uint8), rgb.reshape(h, w * 3)], axis=1)

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6))
            + chunk(b"IEND", b""))


def _colourise(values: np.ndarray, ramp: list[tuple[float, tuple[int, int, int]]]) -> np.ndarray:
    img = np.empty(values.shape + (3,), dtype=np.uint8)
    img[...] = BACKGROUND
    for threshold, colour in ramp:
        img[values >= threshold] = colour
    return img


def _disc(img: np.ndarray, cy: float, cx: float, r: float, colour) -> None:
    yy, xx = np.ogrid[: img.shape[0], : img.shape[1]]
    img[(yy - cy) ** 2 + (xx - cx) ** 2 <= r * r] = colour


def _ring(img: np.ndarray, cy: float, cx: float, ry: float, rx: float, width: float,
          colour) -> None:
    """Ellipse outline: a geographic circle is not a pixel circle (cells are not square km)."""
    yy, xx = np.ogrid[: img.shape[0], : img.shape[1]]
    d = np.sqrt(((yy - cy) / ry) ** 2 + ((xx - cx) / rx) ** 2)   # 1.0 on the ring
    img[np.abs(d - 1.0) * min(rx, ry) <= width / 2] = colour


def _line(img: np.ndarray, y0: float, x0: float, y1: float, x1: float, width: float,
          colour) -> None:
    n = int(max(abs(y1 - y0), abs(x1 - x0))) + 1
    for t in np.linspace(0.0, 1.0, n):
        _disc(img, y0 + (y1 - y0) * t, x0 + (x1 - x0) * t, width / 2, colour)


def render(frame: Frame, home_lat: float, home_lon: float, *, alert_radius_km: float,
           hit_radius_km: float, motion: Motion | None = None) -> bytes:
    """PNG bytes of the frame's box with the house, radii and motion vector drawn on top."""
    ramp = _DBZ_RAMP if frame.product == "reflectivity" else _RATE_RAMP
    img = np.repeat(np.repeat(_colourise(frame.values, ramp), SCALE, axis=0), SCALE, axis=1)

    dlat = abs(float(frame.lats[0] - frame.lats[1])) if len(frame.lats) > 1 else 0.01
    dlon = abs(float(frame.lons[1] - frame.lons[0])) if len(frame.lons) > 1 else 0.01
    px_per_km_y = SCALE / (dlat * KM_PER_DEG)
    px_per_km_x = SCALE / (dlon * KM_PER_DEG * np.cos(np.radians(home_lat)))
    # rows increase southward; house position in pixels
    cy = (float(frame.lats[0]) - home_lat) / dlat * SCALE + SCALE / 2
    cx = (home_lon - float(frame.lons[0])) / dlon * SCALE + SCALE / 2

    rings = ((hit_radius_km, 1.0, HIT_RING), (alert_radius_km, 1.5, ALERT_RING))
    for r_km, width, colour in rings:
        _ring(img, cy, cx, r_km * px_per_km_y, r_km * px_per_km_x, width, colour)

    if motion is not None and frame.values.any():
        j, i = np.unravel_index(int(np.argmax(frame.values)), frame.values.shape)
        y0, x0 = j * SCALE + SCALE / 2, i * SCALE + SCALE / 2
        y1 = y0 - motion.v_km_per_min * VECTOR_MINUTES * px_per_km_y
        x1 = x0 + motion.u_km_per_min * VECTOR_MINUTES * px_per_km_x
        _line(img, y0, x0, y1, x1, 3.0, VECTOR)
        _disc(img, y1, x1, 4.0, VECTOR)

    arm = 3 * SCALE
    _line(img, cy, cx - arm, cy, cx + arm, 2.0, HOUSE)
    _line(img, cy - arm, cx, cy + arm, cx, 2.0, HOUSE)
    _disc(img, cy, cx, 1.5, HOUSE)
    return encode_png(img)
