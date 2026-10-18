"""Lifecycle of the Vein server process: start, stop, restart, update, crash watchdog, log tailing."""
from __future__ import annotations

import logging
import json
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from enum import Enum

import psutil

from .. import VsmError, __version__
from ..core.network import clean_dll_search
from ..core.paths import IS_WINDOWS
from .usage import Usage

log = logging.getLogger("vsm.server")

SAVE_START = "LogVeinSaveGame: Saving all objects"
SAVE_FINISH = ("LogVeinSaveGame: Saved to slot Server", "LogVeinSaveGame: Saved autosave game to disk")
READY_MARKERS = ("LogRamjetNetworking: Heartbeating", "SDR RelayNetworkStatus: avail=OK")
RE_JOIN = re.compile(r"Name=(.*?)\?.*ID=(\d+)")
RE_LEAVE = re.compile(r"Ended auth session for ID (\d+)")
RE_CHAT = re.compile(r"LogVeinChat: \[.*?\] (.*?): (.*)")
RE_KILL = re.compile(r"LogVein: Display: (.*?) killed (.*)")

STARTING_TIMEOUT = 120  # considered online after this even if the ready marker never shows
SAVE_WAIT = 60
STOP_WAIT = 45
CRASH_LOOP = (5, 600)  # give up auto-restarting after 5 crashes in 10 minutes
# Starts and stops a person asked for. Only these change whether the server should be running when
# the manager next starts; restarts by the scheduler, the watchdog, an update or Windows shutting
# down do not.
BY_HAND = {"USER", "DISCORD", "SETUP"}


class State(str, Enum):
    OFFLINE = "OFFLINE"
    STARTING = "STARTING"
    ONLINE = "ONLINE"
    STOPPING = "STOPPING"
    RESTARTING = "RESTARTING"
    UPDATING = "UPDATING"


class ServerController:
    def __init__(self, manager):
        self.m = manager
        self.state = State.OFFLINE
        self.pid: int | None = None
        self.proc: subprocess.Popen | None = None
        self.usage = Usage()
        self.started_at: float | None = None
        self.task: str | None = None
        self.crash_count = 0
        self.save_active = False
        self.ready = False
        self.sessions: dict[str, str] = {}
        self._crashes: deque[float] = deque(maxlen=CRASH_LOOP[0])
        # Progress of the guided first-time setup, shown by the UI: [{"name", "state"}], state is
        # pending | running | done | failed.
        self.setup_steps: list[dict] = []
        self.setup_error: str | None = None
        self._op = threading.Lock()
        self._stopping = threading.Event()

    # ---------- public requests (return immediately) ----------

    def request_start(self, trigger: str = "USER") -> None:
        self._submit("Starting", lambda: self._do_start(trigger))

    def request_stop(self, trigger: str = "USER") -> None:
        self._submit("Stopping", lambda: self._do_stop(trigger))

    def request_restart(self, trigger: str = "USER") -> None:
        self._submit("Restarting", lambda: self._do_restart(trigger))

    def request_update(self, validate: bool = False) -> None:
        if self.running():
            raise VsmError("Stop the server before updating.", 409)
        self._submit("Validating" if validate else "Updating", lambda: self._do_update(validate))

    def request_auto_update(self) -> None:
        """Stop, update and start again; only called when the server is online and empty."""
        self._submit("Updating", self._do_auto_update)

    def request_setup(self, plan: dict) -> None:
        """Guided setup. `plan` has already been validated by the API layer."""
        self._submit("Setting up", lambda: self._do_setup(plan))

    def running(self) -> bool:
        return self.pid is not None

    def status(self) -> dict:
        usage = self.usage.latest()
        nxt = self.m.scheduler.next_restart
        return {
            "state": self.state.value,
            "task": self.task,
            "pid": self.pid,
            "uptime": int(time.time() - self.started_at) if self.pid and self.started_at else 0,
            "ram_mb": usage["ram_mb"],
            "usage": usage,
            "players": [{"id": i, "name": n} for i, n in self.sessions.items()],
            "max_players": self.m.config.get().launch.max_players,
            "crash_count": self.crash_count,
            "save_active": self.save_active,
            "installed": self.m.paths.server_exe.exists(),
            "build_id": self.m.steam.build_id(),
            "game_update": self.m.game_updates.status(),
            "manager_update": self.m.self_update.status(),
            "next_restart": nxt.timestamp() if nxt else None,
            "platform": "windows" if IS_WINDOWS else "linux",
            # UE4SS only loads into the Windows server build; UPnP cannot see the router from inside Docker.
            "capabilities": {"ue4ss": IS_WINDOWS, "upnp": self.m.upnp.available},
            "service_mode": self.m.service_mode,
            "setup": {
                # No game server yet: the UI opens the guided setup ("skip for now" only lasts for that window).
                "needed": not self.m.paths.server_exe.exists(),
                "running": self.task == "Setting up",
                "steps": self.setup_steps,
                "error": self.setup_error,
            },
            "version": __version__,
        }

    # ---------- threads ----------

    def start_threads(self) -> None:
        threading.Thread(target=self._boot, name="boot", daemon=True).start()
        threading.Thread(target=self._monitor, name="monitor", daemon=True).start()
        threading.Thread(target=self._tail_log, name="log-tail", daemon=True).start()

    def shutdown(self, stop_game: bool | None = None) -> None:
        """Called when the manager exits. `stop_game` None means "whatever VSM_STOP_SERVER_ON_EXIT says"."""
        self._stopping.set()
        self.m.steam.cancel()
        if stop_game is None:
            stop_game = os.environ.get("VSM_STOP_SERVER_ON_EXIT", "").lower() in ("1", "true", "yes")
        if stop_game:
            with self._op:  # wait for any in-flight operation
                if self.running():
                    self._do_stop("SHUTDOWN")

    # ---------- whether the server should be running ----------

    @property
    def _wanted_file(self):
        return self.m.paths.data / "server-state.json"

    def wanted(self) -> bool:
        """False once somebody stopped the server by hand, until somebody starts it again.
        "Start server with the manager" then waits for them instead of starting it at the next
        Windows restart or manager update. True when nothing has been recorded yet."""
        try:
            return json.loads(self._wanted_file.read_text(encoding="utf-8")).get("wanted") is not False
        except (OSError, ValueError, AttributeError):
            return True

    def _note_wanted(self, wanted: bool, trigger: str) -> None:
        if trigger not in BY_HAND or wanted == self.wanted():
            return
        try:
            temp = self._wanted_file.with_suffix(".tmp")
            temp.write_text(json.dumps({"wanted": wanted, "by": trigger, "at": time.time()}), encoding="utf-8")
            os.replace(temp, self._wanted_file)
        except OSError as e:
            log.warning("could not record whether the server should run: %s", e)

    def _boot(self) -> None:
        pid = self._find_pid()
        if pid:
            self._attach(pid)
            return
        auto_install = os.environ.get("VSM_AUTO_INSTALL", "").lower() in ("1", "true", "yes")
        auto_start = self.m.config.get().behavior.auto_start
        installed = self.m.paths.server_exe.exists()
        if auto_start and not self.wanted():
            auto_start = False
            self.m.event("SYSTEM", "The game server was stopped by hand, so it was not started with the manager. Start it to have it start with the manager again.")

        def install():
            self._do_update(False)
            if auto_start and self.m.paths.server_exe.exists():
                self._do_start("AUTOSTART")

        if auto_install and not installed:
            self.m.event("SYSTEM", "Server not installed; auto-install starting.")
            self._try(lambda: self._submit("Installing", install))
        elif auto_start and installed:
            self._try(self.request_start, "AUTOSTART")

    def _monitor(self) -> None:
        while not self._stopping.wait(2):
            try:
                self._tick()
            except Exception:
                log.exception("monitor tick failed")

    def _tick(self) -> None:
        self.usage.sample(self.pid)
        if self.pid and self.state in (State.STARTING, State.ONLINE):
            if not self._alive():
                self._on_crash()
            elif self.state == State.STARTING and (self.ready or time.time() - self.started_at > STARTING_TIMEOUT):
                self._set_state(State.ONLINE)
                self.m.event("SERVER", "Server is online.")
            elif self.state == State.ONLINE:
                limit = self.m.config.get().behavior.ram_limit_gb
                if limit and not self.task and psutil.Process(self.pid).memory_info().rss > limit * 1024**3:
                    self.m.event("RESOURCE", f"Memory limit of {limit} GB exceeded; restarting.")
                    self.m.discord.notify("WARN", "High RAM usage. Restarting...")
                    self._try(self.request_restart, "RAM_LIMIT")
        self.publish_status()

    def _on_crash(self) -> None:
        self._clear_process()
        self._set_state(State.OFFLINE)
        self.crash_count += 1
        self._crashes.append(time.time())
        self.m.event("WATCHDOG", "Server process exited unexpectedly.")
        self.m.discord.notify("CRASH", "Crash detected.")
        if not self.m.config.get().behavior.keep_alive:
            return
        if len(self._crashes) == CRASH_LOOP[0] and time.time() - self._crashes[0] < CRASH_LOOP[1]:
            hint = " UE4SS is on: try turning it off on the Mods page to rule out a script mod." if self.m.ue4ss.enabled else ""
            self.m.event("WATCHDOG", "Crash loop detected; automatic restart disabled until the next manual start." + hint)
            return

        def revive():
            if not self._stopping.wait(10):
                self._do_start("WATCHDOG")

        self._try(lambda: self._submit("Recovering", revive))

    def _tail_log(self) -> None:
        """Follow Vein.log across restarts. The server replaces the file on every launch."""
        path = self.m.paths.game_log
        ident, pos, buf = None, None, b""  # pos None: manager just started, skip existing output
        while not self._stopping.wait(0.5):
            if self.started_at and time.time() - self.started_at < 3:
                continue  # let a freshly launched server rotate its log first
            try:
                # Opened per poll and never held: on Windows an open handle would
                # stop the server from rotating its log at startup.
                with open(path, "rb") as f:
                    st = os.fstat(f.fileno())
                    size = f.seek(0, os.SEEK_END)
                    if pos is None:
                        pos = size
                    elif (st.st_dev, st.st_ino) != ident or size < pos:
                        pos, buf = 0, b""
                    ident = (st.st_dev, st.st_ino)
                    f.seek(pos)
                    data = f.read()
            except FileNotFoundError:
                ident, pos, buf = None, 0, b""
                continue
            except OSError:
                continue
            pos += len(data)
            *lines, buf = (buf + data).split(b"\n")
            for raw in lines:
                try:
                    self._on_line(raw.decode("utf-8", errors="replace").strip("﻿ \t\r"))
                except Exception:
                    log.exception("log line handling failed")

    def _on_line(self, line: str) -> None:
        if not line:
            return
        self.m.hub.publish("log", line)
        if not self.ready and any(marker in line for marker in READY_MARKERS):
            self.ready = True
        if SAVE_START in line:
            self.save_active = True
        elif any(s in line for s in SAVE_FINISH):
            self.save_active = False

        if m := RE_JOIN.search(line):
            name, sid = m.group(1), m.group(2)
            if sid not in self.sessions:
                self.sessions[sid] = name
                self.ready = True
                self.m.history.seen(sid, name)
                self.m.event("PLAYER", f"{name} joined.")
                self.m.discord.chat(f"🟢 **{name}** has joined the server. (Players: {len(self.sessions)})", 3066993)
        elif m := RE_LEAVE.search(line):
            name = self.sessions.pop(m.group(1), None)
            if name:
                self.m.event("PLAYER", f"{name} left.")
                self.m.discord.chat(f"🔴 **{name}** has disconnected. (Players: {len(self.sessions)})", 15158332)
        elif m := RE_CHAT.search(line):
            self.m.discord.chat(f"💬 **{m.group(1)}**: {m.group(2)}", 3447003)
        elif m := RE_KILL.search(line):
            self.m.discord.chat(f"💀 **{m.group(2)}** was killed by **{m.group(1)}**", 15158332)

    # ---------- operations (run on a worker thread, one at a time) ----------

    def _submit(self, name: str, fn) -> None:
        if not self._op.acquire(blocking=False):
            raise VsmError("Another operation is already in progress.", 409)
        self.task = name

        def run():
            try:
                fn()
            except VsmError as e:
                self.m.event("ERROR", f"{name} failed: {e}")
            except Exception as e:
                log.exception("%s failed", name)
                self.m.event("ERROR", f"{name} failed: {e}")
            finally:
                # Whatever happened, leave the state matching reality.
                if not self._alive():
                    self._clear_process()
                    self.state = State.OFFLINE
                elif self.state not in (State.STARTING, State.ONLINE):
                    self.state = State.ONLINE
                self.task = None
                self._op.release()
                self.publish_status()

        threading.Thread(target=run, name=f"op-{name}", daemon=True).start()
        self.publish_status()

    def _do_start(self, trigger: str) -> None:
        if self._stopping.is_set():
            return  # the manager is exiting; an in-flight restart must not relaunch the game
        self.m.event(trigger, "Start requested.")
        self._note_wanted(True, trigger)
        if pid := self._find_pid():
            self._attach(pid)
            return
        paths = self.m.paths
        if not paths.server_exe.exists():
            raise VsmError("The server is not installed. Run Install / Update first.")
        cfg = self.m.config.get()
        if cfg.behavior.update_on_start and trigger != "WATCHDOG":
            self._set_state(State.UPDATING)
            if not self.m.steam.update():
                self.m.event("UPDATE", "Update failed; launching the installed build anyway.")
        self.m.game.flush_bans()
        self.m.game.apply_launch(cfg)
        self.m.steam.fix_steamclient()
        self.m.request_firewall_sync()
        if self.m.ue4ss.enabled and self.m.ue4ss.build_changed():
            self.m.event("UE4SS", "The game was updated after UE4SS was installed. If the server crashes, update UE4SS or turn it off.")

        url = cfg.launch.map + (f"?SessionName={cfg.launch.session_name}" if cfg.launch.session_name else "")
        cmd = [str(paths.server_exe)] + ([] if IS_WINDOWS else ["Vein"]) + [
            url,
            f"-Port={cfg.launch.port}",
            f"-QueryPort={cfg.launch.query_port}",
            f"-MaxPlayers={cfg.launch.max_players}",
        ]
        if cfg.rcon.enabled:
            cmd += ["-RconEnabled=true", f"-RconPort={cfg.rcon.port}", f"-RconPassword={cfg.rcon.password}"]
        if cfg.launch.single_thread:
            cmd.append("-onethread")
        cmd += ["-log"] + shlex.split(cfg.launch.extra_args)

        if IS_WINDOWS:
            extra = {"creationflags": subprocess.CREATE_NO_WINDOW}
        else:
            os.chmod(paths.server_exe, 0o755)
            extra = {"start_new_session": True}  # keep the manager's Ctrl+C away from the server
        self.ready = False
        self.save_active = False
        self.sessions.clear()
        with clean_dll_search():
            self.proc = subprocess.Popen(
                cmd, cwd=paths.server, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **extra
            )
        self.pid = self.proc.pid
        self.started_at = time.time()
        self._set_state(State.STARTING)
        self.m.event("SERVER", f"Server process started (PID {self.pid}).")
        self.m.upnp.open()
        self.m.discord.notify("START", "Server starting...")

    def _do_stop(self, trigger: str, final: State = State.OFFLINE) -> None:
        self.m.event(trigger, "Stop requested.")
        if final == State.OFFLINE:  # a restart stops on its way to starting again
            self._note_wanted(False, trigger)
        if not self.pid or not self._alive():
            self._clear_process()
            self._set_state(final)
            return
        if final == State.OFFLINE:
            self._set_state(State.STOPPING)

        # Never interrupt a save in progress: that is how worlds get corrupted.
        deadline = time.time() + SAVE_WAIT
        while self.save_active and time.time() < deadline:
            time.sleep(1)
        if self.save_active:
            self.m.event("SENTINEL", "Save did not finish in time; stopping anyway.")

        self._terminate(self.pid)
        self._clear_process()
        self.m.game.flush_bans()
        self.m.upnp.close()
        self._set_state(final)
        self.m.event("SERVER", "Server stopped.")
        self.m.discord.notify("STOP", "Server stopped.")
        if self.m.config.get().backups.on_stop:
            try:
                self.m.backups.create("ON_STOP")
            except VsmError as e:
                self.m.event("BACKUP", f"Backup on stop skipped: {e}")

    def _do_restart(self, trigger: str) -> None:
        self._set_state(State.RESTARTING)
        self._do_stop(trigger, final=State.RESTARTING)
        time.sleep(5)
        self._do_start(trigger)

    def _do_auto_update(self) -> None:
        if self.sessions:
            self.m.event("UPDATE", "Someone joined: the automatic update is put off.")
            return
        self._set_state(State.RESTARTING)
        self._do_stop("AUTO_UPDATE", final=State.RESTARTING)
        self._set_state(State.UPDATING)
        self.m.hub.clear("steam")
        if not self.m.steam.update():
            self.m.event("UPDATE", "The automatic update failed; starting the installed build again.")
        self._do_start("AUTO_UPDATE")

    def _do_setup(self, plan: dict) -> None:
        """Save the chosen settings, install the game server and bring it up, one step at a time.

        Runs here rather than in the browser so that closing the window does not interrupt it.
        A failed step stops the sequence; sending the same plan again retries from the start
        (saving is repeatable and SteamCMD resumes a partial download).
        """
        def save() -> None:
            cfg = self.m.config.update(plan["config"])
            self.m.game.write(plan["game"])
            on = lambda value: "on" if value else "off"  # noqa: E731
            # What was chosen, for anyone reading the log later. No passwords.
            self.m.event("SETUP", (
                f"Settings saved: server \"{plan['game'].get('server_name', '')}\", ports {cfg.launch.port}/{cfg.launch.query_port} UDP, "
                f"{cfg.launch.max_players} players, branch {cfg.behavior.steam_branch}, UPnP {on(cfg.network.upnp)}, "
                f"remote access {on(cfg.network.remote_access)}, join password {'set' if plan['game'].get('password') else 'none'}, "
                f"auto-start {on(cfg.behavior.auto_start)}, restart after crash {on(cfg.behavior.keep_alive)}, "
                f"backups {'every ' + str(cfg.backups.interval_hours) + ' h' if cfg.backups.interval_hours else 'off'}."
            ))

        def download() -> None:
            if self._find_pid():
                raise VsmError("A game server is already running.")
            self._set_state(State.UPDATING)
            self.m.hub.clear("steam")
            ok = self.m.steam.update()
            self._set_state(State.OFFLINE)
            if not ok:
                raise VsmError("SteamCMD could not download the server. The output below has the details.")

        steps = [("Saving your settings", save), ("Downloading the game server", download)]
        if plan.get("ue4ss") and self.m.ue4ss.supported:
            steps.append(("Installing UE4SS", self.m.ue4ss.install_from_github))
        if self.m.service_mode:
            steps.append(("Updating Windows Firewall", self.m.request_firewall_sync))
        if plan.get("start"):
            steps.append(("Starting the server", lambda: self._do_start("SETUP")))

        self.setup_error = None
        self.setup_steps = [{"name": name, "state": "pending"} for name, _ in steps]
        self.m.event("SETUP", "Guided setup started: " + ", ".join(name for name, _ in steps) + ".")
        began = time.monotonic()
        for index, (name, action) in enumerate(steps):
            self.setup_steps[index]["state"] = "running"
            self.publish_status()
            started = time.monotonic()
            try:
                action()
            except Exception as e:
                if not isinstance(e, VsmError):
                    log.exception("setup step failed: %s", name)  # keep the traceback; the event below has only the message
                log.warning("setup stopped at '%s' after %.0f s", name, time.monotonic() - started)
                self.setup_steps[index]["state"] = "failed"
                self.setup_error = f"{name} failed: {e}"
                raise VsmError(self.setup_error)  # recorded as an ERROR event by _submit
            self.setup_steps[index]["state"] = "done"
            self.m.event("SETUP", f"{name}: done ({time.monotonic() - started:.0f} s).")
        self.m.event("SETUP", f"Guided setup finished in {time.monotonic() - began:.0f} s.")

    def _do_update(self, validate: bool) -> None:
        if self._find_pid():
            raise VsmError("Stop the server before updating.", 409)
        self._set_state(State.UPDATING)
        self.m.hub.clear("steam")
        ok = self.m.steam.update(validate)
        self.m.event("UPDATE", "Server files are up to date." if ok else "SteamCMD failed; see the Install / Update page.")
        self._set_state(State.OFFLINE)

    # ---------- process helpers ----------

    def _attach(self, pid: int) -> None:
        self.pid, self.proc = pid, None
        try:
            self.started_at = psutil.Process(pid).create_time()
        except psutil.Error:
            self.started_at = time.time()
        self.ready = True
        self._set_state(State.ONLINE)
        self.m.event("SYSTEM", f"Attached to running server (PID {pid}).")
        self.m.upnp.open()

    def _find_pid(self) -> int | None:
        """Find a server already running from *this* install (other installs on the host are ignored)."""
        exe_path = self.m.paths.server_exe
        target = os.path.normcase(str(exe_path))
        for p in psutil.process_iter(["pid", "name", "status"]):
            # Name first: resolving every process's exe path is slow on Windows.
            if p.info.get("name") != exe_path.name or p.info.get("status") == psutil.STATUS_ZOMBIE:
                continue
            try:
                if os.path.normcase(os.path.realpath(p.exe())) == target:
                    return p.info["pid"]
            except psutil.Error:
                pass
        return None

    def _alive(self) -> bool:
        if not self.pid:
            return False
        if self.proc is not None:
            return self.proc.poll() is None
        try:
            if psutil.Process(self.pid).status() != psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return False
        if not IS_WINDOWS:
            # After the manager restarted in place the server is still its child: collect it.
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
        return False

    def _terminate(self, pid: int) -> None:
        try:
            p = psutil.Process(pid)
            children = p.children(recursive=True)
            # Linux builds shut down cleanly on SIGINT. Windows has no equivalent for a
            # windowless process, so (as before) we wait for saves and then terminate.
            p.terminate() if IS_WINDOWS else p.send_signal(signal.SIGINT)
            try:
                self.proc.wait(STOP_WAIT) if self.proc else p.wait(STOP_WAIT)
            except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                self.m.event("SERVER", "Server did not exit in time; killing it.")
                for victim in [p, *children]:
                    try:
                        victim.kill()
                    except psutil.Error:
                        pass
                if self.proc:
                    self.proc.wait(10)
        except psutil.NoSuchProcess:
            pass

    def _clear_process(self) -> None:
        self.pid, self.proc, self.started_at = None, None, None
        self.save_active = False
        self.sessions.clear()

    def _set_state(self, state: State) -> None:
        self.state = state
        self.publish_status()

    def publish_status(self) -> None:
        self.m.hub.publish("status", self.status())

    @staticmethod
    def _try(fn, *args) -> None:
        try:
            fn(*args)
        except VsmError:
            pass
