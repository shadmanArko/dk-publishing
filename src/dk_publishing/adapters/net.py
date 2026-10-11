"""Public-address checks shared by everything that fetches or calls a URL a client supplied."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence

Resolver = Callable[[str], Sequence[str]]


def resolve_host(host: str) -> list[str]:
    try:
        return sorted(
            {str(i[4][0]) for i in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
        )
    except OSError:
        return []


def is_public(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return ip.is_global and not ip.is_multicast
