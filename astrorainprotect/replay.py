"""Run the detector over recorded frames without network or ntfy."""

from __future__ import annotations

import logging
import tempfile
from collections import deque
from datetime import datetime
from pathlib import Path

from astrorainprotect.config import Config
from astrorainprotect.frame import Frame
from astrorainprotect.mrms import PRODUCTS
from astrorainprotect.state import State

log = logging.getLogger("astrorainprotect")


class DryRunNotifier:
    def send(self, title: str, message: str) -> bool:
        log.info("WOULD SEND %s: %s", title, message)
        return True


def load_frames(directory: Path | str) -> list[Frame]:
    frames = [Frame.load(p) for p in sorted(Path(directory).glob("*.npz"))]
    return sorted(frames, key=lambda f: (f.valid_time, f.product))


class ReplaySource:
    def __init__(self, frames: list[Frame], cache: int = 5) -> None:
        self._all = {p: [f for f in frames if f.product == p] for p in PRODUCTS}
        self._hist: dict[str, deque[Frame]] = {p: deque(maxlen=cache) for p in PRODUCTS}

    def frames(self, product: str) -> list[Frame]:
        return list(self._hist[product])

    def fetch_latest(self, product: str, now: datetime) -> Frame | None:
        candidates = [f for f in self._all[product] if f.valid_time <= now]
        if not candidates:
            return None
        newest = candidates[-1]
        hist = self._hist[product]
        if not hist or hist[-1].valid_time != newest.valid_time:
            hist.append(newest)
        return newest


def run_replay(cfg: Config, replay_dir: Path | str) -> int:
    from astrorainprotect.app import App, run_cycle  # local import avoids a cycle

    frames = load_frames(replay_dir)
    times = sorted({f.valid_time for f in frames})
    log.info("replay: %d frames, %d distinct times from %s", len(frames), len(times), replay_dir)
    clock = {"t": times[0] if times else None}
    with tempfile.TemporaryDirectory() as tmp:
        app = App(cfg=cfg, radar=ReplaySource(frames), notifier=DryRunNotifier(),
                  state=State(Path(tmp) / "state", Path(tmp) / "tmp"), scope_check=lambda: [],
                  pirate=None, clock=lambda: clock["t"])
        for t in times:
            clock["t"] = t
            run_cycle(app)
    return len(times)
