"""Docker HEALTHCHECK entry point: is the poll loop still touching its heartbeat?"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Mapping

from astrorainprotect.state import State


def main(env: Mapping[str, str], now: float) -> int:
    poll = int(env.get("POLL_SEC", "180") or 180)
    age = State(env.get("STATE_DIR", "/state") or "/state").heartbeat_age_sec(now)
    return 0 if age is not None and age <= 3 * poll else 1


if __name__ == "__main__":
    sys.exit(main(os.environ, time.time()))
