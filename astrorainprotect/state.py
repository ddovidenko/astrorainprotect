"""Latch, repeat timer, test marker, heartbeat. All file-backed so restarts keep the latch."""

from __future__ import annotations

import os
from pathlib import Path


class State:
    def __init__(self, state_dir: Path | str, tmp_dir: Path | str = "/tmp") -> None:
        self._dir = Path(state_dir)
        self._latch = self._dir / "alerted"
        self._heartbeat = self._dir / "heartbeat"
        self._test_marker = Path(tmp_dir) / "astrorainprotect_test_sent"
        self._scopes = self._dir / "scopes_online"

    def _touch(self, path: Path, now: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        os.utime(path, (now, now))

    @staticmethod
    def _age(path: Path, now: float) -> float | None:
        try:
            return now - path.stat().st_mtime
        except FileNotFoundError:
            return None

    def latched(self) -> bool:
        return self._latch.exists()

    def latch_age_sec(self, now: float) -> float | None:
        return self._age(self._latch, now)

    def set_latch(self, now: float) -> None:
        self._touch(self._latch, now)

    def clear_latch(self) -> None:
        self._latch.unlink(missing_ok=True)

    def test_sent(self) -> bool:
        return self._test_marker.exists()

    def mark_test_sent(self) -> None:
        self._test_marker.parent.mkdir(parents=True, exist_ok=True)
        self._test_marker.touch()

    def clear_test_marker(self) -> None:
        self._test_marker.unlink(missing_ok=True)

    def heartbeat(self, now: float) -> None:
        self._touch(self._heartbeat, now)

    def heartbeat_age_sec(self, now: float) -> float | None:
        return self._age(self._heartbeat, now)

    def last_scopes(self) -> set[str] | None:
        """Hosts that were online at the last poll, or None if never recorded."""
        try:
            text = self._scopes.read_text()
        except FileNotFoundError:
            return None
        return {h for h in text.split(",") if h}

    def set_scopes(self, hosts: set[str]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        self._scopes.write_text(",".join(sorted(hosts)))
