"""CPU and memory of the game server process, and this machine's network traffic, sampled over time."""
from __future__ import annotations

import time
from collections import deque

import psutil

HISTORY = 150  # at one sample every 2 s: the last five minutes
EMPTY = {"t": 0, "cpu": 0.0, "ram_mb": 0, "up": 0, "down": 0}


def _network_bytes() -> tuple[int, int]:
    """Bytes sent and received on real adapters. Per-process figures are not available to an
    unprivileged process on Windows, so this is the whole machine."""
    sent = received = 0
    for name, nic in psutil.net_io_counters(pernic=True).items():
        if name.lower().startswith(("lo", "loopback")):
            continue
        sent += nic.bytes_sent
        received += nic.bytes_recv
    return sent, received


class Usage:
    def __init__(self):
        self.samples: deque[dict] = deque(maxlen=HISTORY)
        self._proc: psutil.Process | None = None
        self._net: tuple[float, int, int] | None = None
        self._cores = psutil.cpu_count() or 1

    def sample(self, pid: int | None) -> None:
        """Take one sample. Call at a steady interval: CPU and network are rates since the last call."""
        cpu, ram = 0.0, 0
        if pid is None:
            self._proc = None
        else:
            try:
                if self._proc is None or self._proc.pid != pid:
                    self._proc = psutil.Process(pid)
                    self._proc.cpu_percent(None)  # the first reading only starts the clock
                else:
                    # psutil reports per core (400% = four busy cores); show a share of the whole machine.
                    cpu = min(100.0, self._proc.cpu_percent(None) / self._cores)
                ram = round(self._proc.memory_info().rss / 1024**2)
            except psutil.Error:
                self._proc = None
        up = down = 0
        now = time.monotonic()
        try:
            sent, received = _network_bytes()
            if self._net:
                then, old_sent, old_received = self._net
                elapsed = max(now - then, 0.001)
                # A counter that went backwards (adapter reset) reads as no traffic rather than a spike.
                up = max(0, round((sent - old_sent) / elapsed))
                down = max(0, round((received - old_received) / elapsed))
            self._net = (now, sent, received)
        except (OSError, RuntimeError):
            self._net = None
        self.samples.append({"t": round(time.time()), "cpu": round(cpu, 1), "ram_mb": ram, "up": up, "down": down})

    def latest(self) -> dict:
        return self.samples[-1] if self.samples else dict(EMPTY)

    def history(self) -> list[dict]:
        return list(self.samples)
