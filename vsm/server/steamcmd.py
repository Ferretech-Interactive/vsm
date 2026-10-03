"""SteamCMD bootstrap and Vein server install/update, on Windows and Linux."""
from __future__ import annotations

import io
import logging
import os
import re
import shutil
import subprocess
import tarfile
import threading
import urllib.request
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

from ..core.network import clean_dll_search
from ..core.paths import IS_WINDOWS, VEIN_APP_ID

STEAMCMD_URL = (
    "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
    if IS_WINDOWS
    else "https://steamcdn-a.akamaihd.net/client/installer/steamcmd_linux.tar.gz"
)
UPDATE_TIMEOUT = 2 * 60 * 60  # a first install is several GB


class SteamCmd:
    def __init__(self, manager):
        self.m = manager
        override = os.environ.get("VSM_STEAMCMD_PATH")
        self.exe = Path(override) if override else manager.paths.steamcmd / ("steamcmd.exe" if IS_WINDOWS else "steamcmd.sh")
        self._proc: subprocess.Popen | None = None
        self.lock = threading.Lock()  # SteamCMD cannot run twice from one folder
        # Everything SteamCMD prints is also kept on disk: the live view only holds the last lines
        # and is lost when the manager restarts, which is exactly when a failed install is looked into.
        self._log = logging.getLogger("vsm.steamcmd.output")
        self._log.propagate = False  # too chatty for manager.log
        for old in list(self._log.handlers):
            self._log.removeHandler(old)
            old.close()
        file = RotatingFileHandler(manager.paths.logs / "steamcmd.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8", delay=True)
        file.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        self._log.addHandler(file)

    def _out(self, line: str) -> None:
        line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", line).rstrip()  # SteamCMD colours its output on Linux
        if line:
            self.m.hub.publish("steam", line)
            self._log.info(line)

    def ensure(self) -> None:
        if self.exe.exists():
            return
        self._out(f"Downloading SteamCMD from {STEAMCMD_URL} ...")
        self.exe.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(STEAMCMD_URL, timeout=60) as r:
            blob = io.BytesIO(r.read())
        if IS_WINDOWS:
            with zipfile.ZipFile(blob) as z:
                z.extractall(self.exe.parent)
        else:
            with tarfile.open(fileobj=blob, mode="r:gz") as t:
                t.extractall(self.exe.parent, filter="data")
        self._out("SteamCMD installed.")

    def update(self, validate: bool = False) -> bool:
        """Install or update the Vein server. Blocks; output is streamed to the hub."""
        self.m.branches.cancel()  # a branch list refresh gives way to the real work
        with self.lock:
            return self._update(validate)

    def _update(self, validate: bool) -> bool:
        try:
            self.ensure()
        except Exception as e:
            self._out(f"ERROR: could not download SteamCMD: {e}")
            return False
        branch = self.m.config.get().behavior.steam_branch
        cmd = [str(self.exe), "+force_install_dir", str(self.m.paths.server), "+login", "anonymous", "+app_update", VEIN_APP_ID]
        if branch != "public":
            cmd += ["-beta", branch]
        if validate:
            cmd.append("validate")
        cmd.append("+quit")
        # SteamCMD's first run only updates itself and can exit non-zero; one retry covers it.
        for attempt in (1, 2):
            code = self._run(cmd)
            if code == 0 and self.m.paths.server_exe.exists():
                self._out(f"Done. Build {self.build_id() or 'unknown'}.")
                return True
            self._out(f"SteamCMD exited with code {code}." + (" Retrying..." if attempt == 1 else ""))
        return False

    def _run(self, cmd: list[str]) -> int:
        self._out("> " + " ".join(cmd))
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        try:
            with clean_dll_search():
                self._proc = subprocess.Popen(
                    cmd, cwd=self.exe.parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, text=True, errors="replace", **flags,
                )
        except OSError as e:
            self._out(f"ERROR: {e}")
            return -1
        proc = self._proc
        killer = threading.Timer(UPDATE_TIMEOUT, proc.kill)
        killer.start()
        try:
            for line in proc.stdout:
                self._out(line)
            return proc.wait()
        finally:
            killer.cancel()
            self._proc = None

    def cancel(self) -> None:
        if self._proc:
            self._proc.kill()

    def build_id(self) -> str | None:
        try:
            m = re.search(r'"buildid"\s+"(\d+)"', self.m.paths.manifest.read_text(errors="replace"))
            return m.group(1) if m else None
        except OSError:
            return None

    def fix_steamclient(self) -> None:
        """Put the Steam client library where the server looks for it (it fails to list otherwise)."""
        root = self.exe.parent
        if IS_WINDOWS:
            pairs = [(root / "steamclient64.dll", self.m.paths.binaries / "steamclient64.dll")]
        else:
            pairs = [
                (root / "linux64" / "steamclient.so", Path.home() / ".steam" / "sdk64" / "steamclient.so"),
                (root / "linux32" / "steamclient.so", Path.home() / ".steam" / "sdk32" / "steamclient.so"),
            ]
        for src, dst in pairs:
            try:
                if src.exists() and not dst.exists():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    self.m.event("SYSTEM", f"Installed {dst.name} for the server.")
            except OSError as e:
                self.m.event("SYSTEM", f"Could not install {dst.name}: {e}")
