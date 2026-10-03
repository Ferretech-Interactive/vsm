"""Thread-safe fan-out of live events (log lines, status, SteamCMD output) to WebSocket clients."""
from __future__ import annotations

import asyncio
import threading
from collections import deque

BUFFERS = {"log": 2000, "steam": 1000, "event": 500}


class Hub:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set[asyncio.Queue] = set()
        self.buffers = {kind: deque(maxlen=size) for kind, size in BUFFERS.items()}
        self._lock = threading.Lock()

    def publish(self, kind: str, data) -> None:
        if kind in self.buffers:
            with self._lock:
                self.buffers[kind].append(data)
        loop = self.loop
        if loop and not loop.is_closed():
            loop.call_soon_threadsafe(self._fanout, {"type": kind, "data": data})

    def _fanout(self, msg: dict) -> None:
        for q in self.clients:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass  # slow client; it will catch up from the next status tick

    def snapshot(self, kind: str) -> list:
        with self._lock:
            return list(self.buffers[kind])

    def clear(self, kind: str) -> None:
        with self._lock:
            self.buffers[kind].clear()
