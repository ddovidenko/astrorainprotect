"""Issue #16: radar snapshot PNG for ntfy attachments."""
import struct
import zlib

import numpy as np
import pytest

from astrorainprotect.motion import Motion
from astrorainprotect.snapshot import SCALE, encode_png, render
from tests.conftest import make_frame

HOME = (29.97, -95.67)


def decode_png(data: bytes) -> np.ndarray:
    """Minimal decoder for the encoder's own output (8-bit RGB, filter 0 rows)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, {}
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        crc = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])[0]
        assert zlib.crc32(kind + body) & 0xFFFFFFFF == crc
        chunks.setdefault(kind, b"")
        chunks[kind] += body
        pos += 12 + n
    w, h, depth, ctype = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, ctype) == (8, 2)
    raw = zlib.decompress(chunks[b"IDAT"])
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(h, 1 + 3 * w)
    assert (rows[:, 0] == 0).all()
    return rows[:, 1:].reshape(h, w, 3)


def test_encode_png_round_trip():
    img = np.zeros((4, 6, 3), dtype=np.uint8)
    img[1, 2] = (10, 200, 30)
    out = decode_png(encode_png(img))
    assert out.shape == (4, 6, 3)
    assert tuple(out[1, 2]) == (10, 200, 30) and tuple(out[0, 0]) == (0, 0, 0)


def grid(n=101):
    return np.zeros((n, n), dtype=np.float32)


def test_render_size_and_echo_colour():
    g = grid()
    g[60:64, 40:44] = 45.0                        # SW of the house
    png = render(make_frame(g, product="reflectivity"), *HOME, alert_radius_km=20.0,
                 hit_radius_km=5.0)
    img = decode_png(png)
    assert img.shape == (101 * SCALE, 101 * SCALE, 3)
    echo = img[62 * SCALE + 1, 42 * SCALE + 1]
    assert echo.max() > 150                      # strong dBZ is a bright colour
    corner = img[2, 2]
    assert corner.max() < 60                     # background stays dark
    assert len(png) < 60_000


def test_render_marks_house_and_radius():
    png = render(make_frame(grid(), product="reflectivity"), *HOME, alert_radius_km=20.0,
                 hit_radius_km=5.0)
    img = decode_png(png)
    c = 50 * SCALE + SCALE // 2
    assert tuple(img[c, c]) == (255, 255, 255)   # crosshair centre on the house
    assert img[c, c + 8].max() > 200             # crosshair arm
    # a ring pixel roughly 20 km east: 20 / (111.195 * cos(lat) * 0.01) cells
    r_cells = 20.0 / (111.195 * np.cos(np.radians(HOME[0])) * 0.01)
    ring = img[c, int(round(c + r_cells * SCALE))]
    assert ring.max() > 80 and tuple(ring) != (0, 0, 0)


def test_render_draws_motion_vector():
    g = grid()
    g[62:65, 38:41] = 40.0
    m = Motion(u_km_per_min=0.6, v_km_per_min=0.6, confidence=0.9, baseline_min=10)
    with_m = decode_png(render(make_frame(g, product="reflectivity"), *HOME, alert_radius_km=20.0,
                               hit_radius_km=5.0, motion=m))
    without = decode_png(render(make_frame(g, product="reflectivity"), *HOME, alert_radius_km=20.0,
                                hit_radius_km=5.0))
    assert (with_m != without).any(axis=2).sum() > 20     # a visible line was drawn


def test_render_preciprate_colours_rain():
    g = grid()
    g[50:53, 50:53] = 5.0                        # mm/h at the house
    img = decode_png(render(make_frame(g, product="preciprate"), *HOME, alert_radius_km=20.0,
                            hit_radius_km=5.0))
    assert img[51 * SCALE + 1, 52 * SCALE + 1].max() > 150


@pytest.mark.parametrize("product", ["reflectivity", "preciprate"])
def test_render_empty_frame_is_valid(product):
    png = render(make_frame(grid(), product=product), *HOME, alert_radius_km=20.0,
                 hit_radius_km=5.0)
    assert decode_png(png).shape[2] == 3
