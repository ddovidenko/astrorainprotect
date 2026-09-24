"""Scope-online gate: is at least one Seestar reachable on its JSON-RPC port?"""

from __future__ import annotations

import socket

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


def online_hosts(hosts: list[tuple[str, int]], timeout: float = 2.0) -> list[str]:
    online = []
    for host, port in hosts:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                online.append(host)
        except OSError:
            continue
    return online
