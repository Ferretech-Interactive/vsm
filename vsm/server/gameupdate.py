"""Notices when Steam has a newer build of the game server than the one installed.

At an interval set in the settings it refreshes the branch list, which SteamCMD fetches in the
background, and compares the configured branch's build with the installed one. A difference is
announced once per build; with the opt-in setting it is also installed, but only while the
server is online with nobody on it.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("vsm.gameupdate")

AUTO_RETRY = 60 * 60  # after an automatic update attempt, leave that build alone for this long


class GameUpdates:
    def __init__(self, manager):
        self.m = manager
        self._asked = time.time()  # the first check comes one interval after the manager starts
        self._announced: tuple[str, str] | None = None
        self._auto_tried: tuple[str, float] | None = None

    def status(self) -> dict:
        behavior = self.m.config.get().behavior
        branches = self.m.branches.status()
        installed = self.m.steam.build_id()
        entry = next((b for b in branches["branches"] if b["name"] == behavior.steam_branch), None)
        latest = str(entry["build_id"]) if entry and entry["build_id"] else None
        return {
            "available": bool(behavior.game_update_check and installed and latest and latest != installed),
            "installed": installed,
            "latest": latest,
            "branch": behavior.steam_branch,
            "checked_at": branches["checked_at"],
        }

    def tick(self) -> None:
        """Called by the scheduler every few seconds; cheap unless a check is due."""
        behavior = self.m.config.get().behavior
        if not behavior.game_update_check or not self.m.paths.server_exe.exists():
            return
        now = time.time()
        if now - self._asked >= behavior.update_check_minutes * 60:
            self._asked = now
            self.m.branches.refresh()  # in the background; a busy SteamCMD just means "try next time"
        status = self.status()
        if not status["available"]:
            return
        key = (status["branch"], status["latest"])
        if key != self._announced:
            self._announced = key
            self.m.event("UPDATE", f"A game server update is available on the {key[0]} branch: build {key[1]} (installed: {status['installed']}).")
            self.m.discord.notify("UPDATE", f"Game server update available: build {key[1]}.")
        if behavior.game_update_auto:
            self._auto_update(status["latest"], now)

    def _auto_update(self, latest: str, now: float) -> None:
        server = self.m.server
        if server.state.value != "ONLINE" or server.task or server.sessions:
            return  # only a running, idle, empty server; a stopped one updates when it is next started
        if self._auto_tried and self._auto_tried[0] == latest and now - self._auto_tried[1] < AUTO_RETRY:
            return
        self._auto_tried = (latest, now)
        self.m.event("UPDATE", f"Nobody is online: updating the game server to build {latest}.")
        try:
            server.request_auto_update()
        except Exception as e:  # another operation got there first; the next tick decides again
            log.info("automatic update not started: %s", e)
