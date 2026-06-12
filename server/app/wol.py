"""Wake-on-LAN magic packet builder/sender (used for server-local broadcasts)."""
from __future__ import annotations

import re
import socket

_MAC_RE = re.compile(r"^([0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")


def normalise_mac(mac: str) -> str:
    cleaned = (mac or "").strip()
    if not _MAC_RE.match(cleaned):
        raise ValueError(f"Invalid MAC address: {mac!r}")
    return cleaned.replace(":", "").replace("-", "").lower()


def build_magic_packet(mac: str) -> bytes:
    mac_bytes = bytes.fromhex(normalise_mac(mac))
    return b"\xff" * 6 + mac_bytes * 16


def send_magic_packet(mac: str, broadcast_ip: str = "255.255.255.255", port: int = 9) -> None:
    packet = build_magic_packet(mac)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(packet, (broadcast_ip or "255.255.255.255", port))
