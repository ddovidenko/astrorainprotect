#!/usr/bin/env python3
"""Record MRMS subset frames around LAT/LON for fixtures and replay.

Usage: LAT=.. LON=.. python scripts/record_frames.py OUT_DIR --minutes 60 --interval 120
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from astrorainprotect.mrms import PRODUCTS, MrmsError, RadarSource


def record(radar, products: list[str], out_dir: Path, *, minutes: int, interval_sec: int,
           clock: Callable[[], datetime], sleep: Callable[[float], None]) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    end = clock().timestamp() + minutes * 60
    while clock().timestamp() < end:
        for product in products:
            try:
                frame = radar.fetch_latest(product, clock())
            except MrmsError as exc:
                print(f"ERROR {exc}", file=sys.stderr)
                continue
            if frame is None or frame.filename() in seen:
                continue
            frame.save(out_dir / frame.filename())
            seen.add(frame.filename())
            written += 1
            print(f"saved {frame.filename()} max={frame.values.max():.1f} "
                  f"missing={frame.missing_fraction:.0%}")
        sleep(interval_sec)
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--minutes", type=int, default=60)
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--products", default=",".join(PRODUCTS))
    args = ap.parse_args(argv)
    lat, lon = float(os.environ["LAT"]), float(os.environ["LON"])
    radar = RadarSource(httpx.Client(), lat, lon)
    n = record(radar, args.products.split(","), args.out_dir, minutes=args.minutes,
               interval_sec=args.interval, clock=lambda: datetime.now(UTC), sleep=time.sleep)
    print(f"wrote {n} frames to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
