"""Scope-online gate: is at least one Seestar reachable on its JSON-RPC port?"""

from __future__ import annotations

import socket
import time
from collections.abc import Callable

DEFAULT_PORT = 4700


def parse_hosts(spec: str) -> list[tuple[str, int]]:
    hosts: list[tuple[str, int]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        host, _, port = item.partition(":")
        try:
            hosts.append((host.strip(), int(port) if port else DEFAULT_PORT))
        except ValueError:
            hosts.append((host.strip(), DEFAULT_PORT))
    return hosts


def probe_hosts(hosts: list[tuple[str, int]], timeout: float = 5.0, retries: int = 1,
                retry_pause: float = 1.0,
                sleep: Callable[[float], None] | None = None) -> dict[str, float | None]:
    """Connect time in seconds per host, or None when no attempt got through.

    A Seestar imaging outdoors on Wi-Fi power save can take seconds to answer a first packet
    while staying perfectly usable over an established connection, so each host gets a
    generous timeout and `retries` further attempts before it counts as not answering. A host
    that is really off refuses or is unroutable at once, so the common case stays fast.
    """
    pause = sleep if sleep is not None else time.sleep
    result: dict[str, float | None] = {}
    for host, port in hosts:
        result[host] = None
        for attempt in range(retries + 1):
            if attempt:
                pause(retry_pause)
            start = time.monotonic()
            try:
                with socket.create_connection((host, port), timeout=timeout):
                    result[host] = time.monotonic() - start
                    break
            except OSError:
                continue
    return result


def online_hosts(hosts: list[tuple[str, int]], timeout: float = 5.0, retries: int = 1) -> list[str]:
    probed = probe_hosts(hosts, timeout=timeout, retries=retries)
    return [host for host, seconds in probed.items() if seconds is not None]
