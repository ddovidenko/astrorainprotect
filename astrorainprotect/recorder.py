"""Save the frames behind an alert to the state dir for replay (#57).

A recording starts on the cycle that sends a new alert, beginning with the frames already in
the cache (up to 30 minutes of history), follows every poll while the alarm stays latched,
and closes a tail after re-arm. Files use the same names as scripts/record_frames.py, so
REPLAY_DIR can point at a recording folder as it is. Any file error is logged, never raised:
a full disk must not cost a notification.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Iterable
from datetime import datetime, timedelta
from pathlib import Path

from astrorainprotect.frame import Frame

log = logging.getLogger("astrorainprotect")

TAIL = timedelta(minutes=30)   # keep saving this long after re-arm, for the recession


class Recorder:
    def __init__(self, root: Path | str, *, keep: int = 10, tail: timedelta = TAIL) -> None:
        self.root = Path(root)
        self.keep = keep
        self.tail = tail
        self.path: Path | None = None
        self._seen: set[str] = set()
        self._close_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.path is not None

    def update(self, now: datetime, outcome: str, frames: Iterable[Frame]) -> None:
        """Drive the recording from one poll cycle's outcome and the frames in the cache."""
        try:
            if not self.active:
                if outcome != "send":
                    return
                self._start(now)
            self._add(frames)
            if outcome in ("send", "repeat"):
                self._close_at = None
            elif outcome == "re-armed":
                self._close_at = now + self.tail
            if self._close_at is not None and now >= self._close_at:
                self.close("tail complete")
        except Exception:
            log.exception("recording failed; alerting is unaffected")

    def close(self, reason: str) -> None:
        if self.path is not None:
            log.info("recording closed (%s): %d frames in %s", reason, len(self._seen), self.path)
        self.path, self._seen, self._close_at = None, set(), None

    def _start(self, now: datetime) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._prune()
        self.path = self.root / now.strftime("%Y%m%d-%H%M")
        self.path.mkdir(exist_ok=True)
        log.info("recording to %s", self.path)

    def _add(self, frames: Iterable[Frame]) -> None:
        assert self.path is not None
        for f in frames:
            name = f.filename()
            if name not in self._seen:
                f.save(self.path / name)
                self._seen.add(name)

    def _prune(self) -> None:
        """Keep the newest keep-1 folders so the one about to start makes `keep`."""
        folders = sorted(p for p in self.root.iterdir() if p.is_dir())
        for old in folders[: max(0, len(folders) - (self.keep - 1))]:
            shutil.rmtree(old, ignore_errors=True)
            log.info("recording removed: %s", old)
