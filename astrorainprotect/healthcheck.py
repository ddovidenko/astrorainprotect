"""Docker HEALTHCHECK entry point: is the poll loop alive, and has anything external answered
it lately? A container with no network keeps polling and looks alive by heartbeat alone (#62)."""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping

from astrorainprotect.state import State

CONTACT_POLLS = 6   # polls without radar, ntfy, Pirate Weather or a scope answering = unhealthy


def main(env: Mapping[str, str], now: float) -> int:
    poll = int(env.get("POLL_SEC", "180") or 180)
    state = State(env.get("STATE_DIR", "/state") or "/state")
    beat = state.heartbeat_age_sec(now)
    contact = state.contact_age_sec(now)
    alive = beat is not None and beat <= 3 * poll
    in_touch = contact is not None and contact < CONTACT_POLLS * poll
    return 0 if alive and in_touch else 1


if __name__ == "__main__":
    sys.exit(main(os.environ, time.time()))
