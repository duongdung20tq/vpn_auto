"""Is the VPN server reachable? Used to connect as soon as the network is back."""
import socket
from urllib.parse import urlsplit


def server_address(url, default_port=443):
    """'vpn.example.test', 'https://vpn.example.test:8443/x' -> (host, port)."""
    parts = urlsplit(url if "://" in url else f"https://{url}")
    return parts.hostname or "", parts.port or default_port


def reachable(url, timeout=3.0):
    host, port = server_address(url)
    if not host:
        return True  # nothing to check against; let Pulse try
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False
