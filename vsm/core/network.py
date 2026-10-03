"""This machine's addresses, and starting child processes cleanly."""
from __future__ import annotations

import contextlib
import json
import socket
import sys
import threading
import time
import urllib.request

from .paths import IS_WINDOWS

UNKNOWN = "unknown"
PUBLIC_IP_TTL = 600

_public: tuple[float, str] = (0.0, UNKNOWN)
_dll_lock = threading.Lock()


def public_ip() -> str:
    """The address the internet sees, asked from ipify and remembered for ten minutes."""
    global _public
    checked, ip = _public
    if ip != UNKNOWN and time.monotonic() - checked < PUBLIC_IP_TTL:
        return ip
    try:
        with urllib.request.urlopen("https://api.ipify.org?format=json", timeout=4) as r:
            ip = str(json.loads(r.read(256).decode())["ip"])
    except Exception:
        return UNKNOWN
    _public = (time.monotonic(), ip)
    return ip


def local_ip() -> str:
    """This machine's address on the local network."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # no packet is sent; this only picks the outgoing interface
        return probe.getsockname()[0]
    except OSError:
        return UNKNOWN
    finally:
        probe.close()


@contextlib.contextmanager
def clean_dll_search():
    """Start a child process without the packaged build's DLL folder.

    The packaged exe adds its own folder to this process's DLL search path and children inherit it:
    the game server would load, and lock, the manager's copy of the VC runtime, which makes an
    upgrade fail while the game server is running.
    """
    bundle = getattr(sys, "_MEIPASS", None)
    if not (IS_WINDOWS and bundle):
        yield
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    with _dll_lock:
        kernel32.SetDllDirectoryW(None)
        try:
            yield
        finally:
            kernel32.SetDllDirectoryW(bundle)
