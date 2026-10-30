"""Block connections to non-public IP addresses in untrusted media workers."""

from __future__ import annotations

import ipaddress
import socket


def _public(address: str) -> bool:
    try:
        return ipaddress.ip_address(address).is_global
    except ValueError:
        return False


def install_public_network_guard() -> None:
    original_getaddrinfo = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def safe_getaddrinfo(host, port, *args, **kwargs):
        if host is None:
            raise OSError("A destination host is required")
        results = original_getaddrinfo(host, port, *args, **kwargs)
        if not results or any(not _public(item[4][0]) for item in results):
            raise OSError("Connections to non-public addresses are blocked")
        return results

    def checked_address(sock: socket.socket, address):
        if sock.family == socket.AF_UNIX:
            return address
        if not isinstance(address, tuple) or not address:
            raise OSError("Unsupported network address")
        host = address[0]
        if _public(host):
            return address
        try:
            ipaddress.ip_address(host)
        except ValueError:
            answers = safe_getaddrinfo(host, address[1], sock.family, sock.type, sock.proto)
            return answers[0][4]
        raise OSError("Connections to non-public addresses are blocked")

    def safe_connect(sock: socket.socket, address):
        return original_connect(sock, checked_address(sock, address))

    def safe_connect_ex(sock: socket.socket, address):
        return original_connect_ex(sock, checked_address(sock, address))

    socket.getaddrinfo = safe_getaddrinfo
    socket.socket.connect = safe_connect
    socket.socket.connect_ex = safe_connect_ex
