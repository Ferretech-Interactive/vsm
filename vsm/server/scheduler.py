"""Scheduled restarts (fixed times and uptime limit), restart warnings and interval backups."""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timedelta

from .. import VsmError
from .controller import State

log = logging.getLogger("vsm.scheduler")


class Scheduler:
    def __init__(self, manager):
        self.m = manager
        self.next_restart: datetime | None = None
        self._last_warning = -1
        self._stopping = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, name="scheduler", daemon=True).start()

    def stop(self) -> None:
        self._stopping.set()

    def _loop(self) -> None:
        while not self._stopping.wait(10):
            try:
                self._tick(datetime.now())
            except Exception:
                log.exception("scheduler tick failed")

    def _next_daily(self, now: datetime) -> datetime | None:
        sched = self.m.config.get().schedule
        best = None
        for t in sched.times:
            hour, minute = map(int, t.split(":"))
            for offset in range(8):
                when = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                if when > now and sched.days[when.weekday()]:
                    best = when if best is None or when < best else best
                    break
        return best

    def _tick(self, now: datetime) -> None:
        cfg = self.m.config.get()
        server = self.m.server
        self.m.upnp.tick()
        self.m.game_updates.tick()
        if server.state != State.ONLINE:
            self.next_restart = None
            return

        if cfg.backups.interval_hours and not server.save_active:
            if now - self.m.backups.last_created >= timedelta(hours=cfg.backups.interval_hours):
                try:
                    self.m.backups.create("INTERVAL")
                except VsmError as e:
                    self.m.backups.last_created = now  # do not retry every 10 seconds
                    self.m.event("BACKUP", f"Interval backup skipped: {e}")

        # The previously announced restart time has arrived.
        if self.next_restart and now >= self.next_restart:
            self.next_restart = None
            self._last_warning = -1
            self.m.event("SCHEDULER", "Scheduled restart.")
            try:
                server.request_restart("SCHEDULER")
            except VsmError:
                pass
            return

        candidates = []
        if cfg.schedule.enabled:
            candidates.append(self._next_daily(now))
        if cfg.schedule.uptime_enabled and server.started_at:
            candidates.append(datetime.fromtimestamp(server.started_at) + timedelta(hours=cfg.schedule.uptime_hours))
        candidates = [c for c in candidates if c]
        self.next_restart = min(candidates) if candidates else None
        if not self.next_restart:
            return

        minutes = math.ceil((self.next_restart - now).total_seconds() / 60)
        if minutes in cfg.schedule.warnings and minutes != self._last_warning:
            self._last_warning = minutes
            self.m.event("SCHEDULER", f"Restart in {minutes} minute(s).")
            self.m.discord.notify("WARN", f"Server restarting in {minutes} minute(s).")
