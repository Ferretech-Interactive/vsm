"""Wires the pieces together. One Manager exists per process."""
from __future__ import annotations

import logging
import os
import sys
import threading
import time
from logging.handlers import RotatingFileHandler

from . import VsmError, __version__
from .api.auth import Auth
from .core.config import ConfigStore
from .core.hub import Hub
from .core.paths import Paths
from .core.selfupdate import SelfUpdate
from .core.updates import Updates
from .game.settings import GameSettings
from .integrations.discord_bot import Discord
from .server.controller import ServerController
from .server.scheduler import Scheduler
from .integrations.steam_users import SteamUsers
from .server.branches import Branches
from .server.gameupdate import GameUpdates
from .server.portcheck import PortCheck
from .server.steamcmd import SteamCmd
from .server.upnp import Upnp
from .storage.backups import Backups
from .storage.mods import Mods
from .storage.players import PlayerHistory
from .storage.profiles import Profiles
from .storage.ue4ss import Ue4ss

log = logging.getLogger("vsm")

# Game.ini values that can be seeded from the environment for unattended installs.
GAME_ENV = {
    "VSM_SERVER_NAME": "server_name",
    "VSM_SERVER_DESCRIPTION": "description",
    "VSM_SERVER_PASSWORD": "password",
    "VSM_SUPER_ADMINS": "super_admins",
    "VSM_ADMINS": "admins",
    "VSM_HTTP_API_PORT": "http_port",
}


class Manager:
    def __init__(self) -> None:
        self.paths = Paths()
        self.paths.ensure()
        self._setup_logging()
        self.hub = Hub()
        self.config = ConfigStore(self.paths.config_file)
        self.auth = Auth(self.paths.auth_file)
        self.game = GameSettings(self.paths)
        self.steam = SteamCmd(self)
        self.branches = Branches(self)
        self.game_updates = GameUpdates(self)
        self.backups = Backups(self)
        self.mods = Mods(self.paths.mods)
        self.logic_mods = Mods(self.paths.logic_mods)  # blueprint mods, loaded by UE4SS
        self.ue4ss = Ue4ss(self)
        self.updates = Updates(self)
        self.upnp = Upnp(self)
        self.portcheck = PortCheck(self)
        # Set by whoever is stopping the manager (the Windows service distinguishes "service
        # stopped" from "Windows is shutting down"); None falls back to VSM_STOP_SERVER_ON_EXIT.
        self.stop_game_on_exit: bool | None = None
        self.pipe = None
        # True when running as the Windows service: sign-in codes over the pipe, firewall kept in step.
        self.service_mode = os.environ.get("VSM_LOCAL_LOGIN", "").lower() == "pipe"
        self.restart_hook = None  # set by the service so the manager can ask to be restarted
        # Linux self-update (core/selfupdate.py): restart in place, optionally as another version.
        self.restart_requested = False
        self.reexec: str | None = None
        self.self_update = SelfUpdate(self)
        # When the desktop window's page last asked for the update to be installed (see api/routes.py).
        self.update_requested_at = 0.0
        self.profiles = Profiles(self)
        self.history = PlayerHistory(self)
        self.steam_users = SteamUsers(self)
        self.discord = Discord(self)
        self.scheduler = Scheduler(self)
        self.server = ServerController(self)

    def _setup_logging(self) -> None:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        if not root.handlers and sys.stderr is not None:  # None in the windowed desktop build
            console = logging.StreamHandler()
            console.setFormatter(fmt)
            root.addHandler(console)
        file = RotatingFileHandler(self.paths.logs / "manager.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8")
        file.setFormatter(fmt)
        root.addHandler(file)
        # The audit trail also gets a file of its own, so it is not buried in (or rotated out with) the rest.
        self._audit = logging.getLogger("vsm.audit")
        for old in list(self._audit.handlers):
            self._audit.removeHandler(old)
            old.close()
        audit = RotatingFileHandler(self.paths.logs / "audit.log", maxBytes=2_000_000, backupCount=10, encoding="utf-8", delay=True)
        audit.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._audit.addHandler(audit)

    def audit(self, message: str, who: str) -> None:
        """Record who did what. Shown in the Event log; written to manager.log and audit.log."""
        self._audit.info("%s (by %s)", message, who)
        self.hub.publish("event", {"time": time.time(), "category": "AUDIT", "message": f"{message} (by {who})"})

    def event(self, category: str, message: str) -> None:
        """Audit trail shown in the UI and written to manager.log."""
        log.info("[%s] %s", category, message)
        self.hub.publish("event", {"time": time.time(), "category": category, "message": message})

    def start(self) -> None:
        log.info("Vein Server Manager v%s | data: %s | server: %s", __version__, self.paths.data, self.paths.server)
        generated = self.auth.bootstrap()
        local_login = os.environ.get("VSM_LOCAL_LOGIN", "").lower()
        desktop = local_login in ("1", "true", "yes", "pipe")
        token_file = self.paths.data / "desktop.token"
        if local_login == "pipe":
            # Windows service: administrators get one-time codes over a named pipe instead.
            from .desktop.localauth import PipeServer

            token_file.unlink(missing_ok=True)
            self.auth.enable_local_codes()
            self.pipe = PipeServer(self.auth.issue_local_code)
            self.pipe.start()
        elif desktop:
            self.auth.enable_local_login(token_file)
        # The desktop app has no console; its window prompts for a password instead.
        if generated and not desktop and sys.stdout is not None:
            # Printed (not logged to file) so it only ever appears on the console / container log.
            print(
                "\n" + "=" * 64 + "\n"
                "  First run: an admin password was generated.\n"
                f"      Password: {generated}\n"
                "  Change it under System > Password, or set VSM_ADMIN_PASSWORD.\n"
                + "=" * 64 + "\n",
                flush=True,
            )
        seed = {key: os.environ[name] for name, key in GAME_ENV.items() if os.environ.get(name)}
        if seed:
            for key in ("super_admins", "admins"):
                if key in seed:
                    seed[key] = [seed[key]]
            try:
                self.game.write(seed)
            except (VsmError, ValueError) as e:
                raise SystemExit(f"Invalid VSM_* environment configuration: {e}")
        self.server.start_threads()
        self.scheduler.start()
        self.discord.start_bot()

    def request_firewall_sync(self) -> None:
        """Ask the elevated helper task to make Windows Firewall match the configured ports."""
        if not self.service_mode:
            return

        def run() -> None:
            from .desktop import system

            if not system.run_firewall_task():
                self.event("FIREWALL", "Could not update the Windows Firewall rule; run Setup again to repair it.")

        threading.Thread(target=run, name="firewall-sync", daemon=True).start()

    def firewall_status(self) -> dict:
        """For the health check: does the firewall rule match the configured game ports?"""
        if os.name != "nt":
            return {"managed": False, "state": None, "ports": []}
        if not self.service_mode:
            return {"managed": False, "state": "unmanaged", "ports": []}
        from .desktop import firewall

        launch = self.config.get().launch
        wanted = firewall.Rule(firewall.GAME_RULE, "UDP", tuple(sorted({launch.port, launch.query_port})), str(self.paths.server_exe))
        current = firewall.current_rule(firewall.GAME_RULE)
        state = "ok" if firewall.in_sync(current, wanted) else ("missing" if current is None else "mismatch")
        return {"managed": True, "state": state, "ports": list(current["ports"]) if current else []}

    def shutdown(self) -> None:
        self.scheduler.stop()
        self.branches.cancel()
        if self.pipe:
            self.pipe.stop()
        self.server.shutdown(self.stop_game_on_exit)
        if self.server.running():
            self.upnp.extend()  # the game outlives the manager: keep the router forwarding to it
