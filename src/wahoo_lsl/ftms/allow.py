"""Allowed-host check for remote FTMS clients, by IP or DNS name."""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from collections.abc import Awaitable, Callable, Sequence

log = logging.getLogger(__name__)

CHECK_TIMEOUT_S = 2.0

Resolver = Callable[[str], Awaitable[list[str]]]


def normalize_ip(text: str) -> str | None:
    """Canonical IP string (IPv4-mapped IPv6 -> IPv4, scope dropped), or None."""
    try:
        addr = ipaddress.ip_address(text.partition("%")[0])
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return str(addr)


async def resolve_host(name: str) -> list[str]:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(name, None)
    except OSError:
        return []
    found = (normalize_ip(info[4][0]) for info in infos)
    return list(dict.fromkeys(ip for ip in found if ip))


async def is_allowed(
    peer_ip: str,
    allowed: Sequence[str],
    resolve: Resolver = resolve_host,
    timeout_s: float = CHECK_TIMEOUT_S,
) -> bool:
    peer = normalize_ip(peer_ip)
    if peer is None:
        return False
    if ipaddress.ip_address(peer).is_loopback:
        return True
    names = []
    for entry in allowed:
        ip = normalize_ip(entry)
        if ip == peer:
            return True
        if ip is None:
            names.append(entry.lower())
    deadline = time.monotonic() + timeout_s
    pending = {asyncio.ensure_future(resolve(name)) for name in names}
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending,
                timeout=max(0.0, deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                log.debug("Host lookup timed out for %d name(s)", len(pending))
                return False
            for task in done:
                try:
                    if peer in {normalize_ip(a) for a in task.result()}:
                        return True
                except Exception as exc:
                    log.debug("Host lookup failed: %s", exc)
        return False
    finally:
        for task in pending:
            task.cancel()
