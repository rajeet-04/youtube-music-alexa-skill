"""Sliding-window rate limits and trusted-proxy client address handling."""

from __future__ import annotations

import ipaddress
import threading
import time
from collections import defaultdict, deque
from typing import Callable, Iterable


class RateLimiter:
    """Per-key sliding window. ``check`` returns seconds to wait, or 0 if allowed."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 10_000) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._hits: dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, limit: int, window: float = 60.0) -> int:
        if limit <= 0:
            return 0
        now = self._clock()
        with self._lock:
            if len(self._hits) > self._max_keys:
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v and now - v[-1] < window})
            hits = self._hits[(bucket, key)]
            while hits and now - hits[0] >= window:
                hits.popleft()
            if len(hits) >= limit:
                return max(1, int(window - (now - hits[0])) + 1)
            hits.append(now)
            return 0


def parse_networks(values: Iterable[str]) -> tuple:
    networks = []
    for value in values:
        value = value.strip()
        if value:
            networks.append(ipaddress.ip_network(value, strict=False))
    return tuple(networks)


def client_address(remote_addr: str | None, forwarded: dict[str, str | None], trusted: tuple) -> str:
    """Return the caller's address, honouring forwarding headers only from trusted peers.

    ``forwarded`` carries ``cf`` (CF-Connecting-IP) and ``xff`` (X-Forwarded-For).
    A direct, untrusted peer can never choose its own rate-limit identity.
    """
    peer = remote_addr or "0.0.0.0"
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in network for network in trusted):
        return peer
    cf = (forwarded.get("cf") or "").strip()
    if cf:
        try:
            return str(ipaddress.ip_address(cf))
        except ValueError:
            pass
    # Walk X-Forwarded-For right to left, skipping our own trusted hops.
    hops = [h.strip() for h in (forwarded.get("xff") or "").split(",") if h.strip()]
    for hop in reversed(hops):
        try:
            ip = ipaddress.ip_address(hop)
        except ValueError:
            return peer
        if not any(ip in network for network in trusted):
            return str(ip)
    return peer
