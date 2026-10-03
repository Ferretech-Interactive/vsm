"""Linux: the manager updates itself from the web UI without stopping the game server.

Nothing here needs root, and the installed program is never modified:

1. The new release's wheel is downloaded from this project's GitHub releases, checked against the
   release's SHA256SUMS.txt, and installed into a fresh virtualenv under `<data>/manager/versions/`.
2. The manager shuts its HTTP server down, leaves the game server running, and replaces its own
   process image with the new version (`os.execv`). The process ID does not change, so systemd and
   Docker see nothing, and the game server stays a child of the same process.
3. Every later start goes through the installed program first (`chain()`), which hands over to the
   newest version that has proved it can start. One that fails is never tried twice.

Trust is GitHub and PyPI over HTTPS, the same as `install.sh`. Releases are not signed yet.
Windows uses Setup.exe instead (`vsm/desktop/updater.py`).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .. import VsmError, __version__
from .updates import REPO_URL, parse

log = logging.getLogger("vsm.selfupdate")

SUPPORTED = sys.platform.startswith("linux") and not getattr(sys, "frozen", False)
DOWNLOADS = REPO_URL + "/releases/download/"
SUMS_NAME = "SHA256SUMS.txt"
MAX_WHEEL = 100 * 1024 * 1024
HEALTHY_AFTER = 20  # seconds a new version must stay up before it is trusted on later starts
PIP_TIMEOUT = 15 * 60
SESSIONS_FILE = "sessions.json"
SESSIONS_MAX_AGE = 120  # seconds; a file older than a restart takes is not from this restart
_WHEEL = re.compile(r"^vein_server_manager-[0-9A-Za-z.]+-py3-none-any\.whl$")


def root() -> Path:
    return Path(os.environ.get("VSM_DATA_DIR") or "data").resolve() / "manager"


# ---------- which version runs ----------

def read_state(folder: Path) -> dict:
    try:
        state = json.loads((folder / "state.json").read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def write_state(folder: Path, state: dict) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    temp = folder / "state.json.tmp"
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temp, folder / "state.json")


def choose(state: dict, own: str, exists=os.path.exists) -> tuple[dict | None, dict]:
    """Which installed version should run instead of this one, and the state to remember.

    Returns (entry or None, new state). An entry is used only if it is newer than the program
    asking, its interpreter is still there, and it has not already failed to start: one that was
    tried and never reported healthy is dropped and recorded as failed.
    """
    mine = parse(own)
    new = {k: state.get(k) for k in ("current", "previous", "failed")}
    for slot in ("current", "previous"):
        entry = new.get(slot)
        if not isinstance(entry, dict):
            new[slot] = None
            continue
        version = parse(str(entry.get("version", "")))
        if version is None or mine is None or version <= mine or not exists(str(entry.get("python", ""))):
            new[slot] = None  # older than the installed program (it was upgraded), or gone
            continue
        if entry.get("tried") and not entry.get("healthy"):
            new["failed"] = {"version": entry.get("version"), "at": time.time()}
            new[slot] = None
            continue
        if slot == "previous":
            new["current"], new["previous"] = entry, None
        entry = dict(entry, tried=True)
        new["current"] = entry
        return entry, new
    return None, new


def chain() -> None:
    """Called first thing by `python -m vsm`: hand over to a newer self-installed version, if any.
    Never raises; with anything wrong it simply runs the installed program."""
    if not SUPPORTED:
        return
    try:
        folder = root()
        if Path(sys.prefix).resolve().is_relative_to(folder):
            return  # this already is a self-installed version; only the installed program decides
        state = read_state(folder)
        if not state:
            return
        entry, new = choose(state, __version__)
        if new != state:
            write_state(folder, new)
        if entry:
            python = str(entry["python"])
            os.execv(python, [python, "-m", "vsm"])
    except Exception:  # noqa: BLE001 - starting the installed program matters more than why
        log.exception("could not hand over to the self-installed version")


# ---------- release assets ----------

def assets(release: dict | None) -> tuple[str, str, str] | None:
    """(wheel name, wheel URL, checksums URL) of a release, or None if it has no Linux package.
    Only this repository's own release downloads are accepted."""
    wheel = sums = None
    for asset in (release or {}).get("assets") or []:
        name, url = str(asset.get("name", "")), str(asset.get("browser_download_url", ""))
        if not url.startswith(DOWNLOADS):
            continue
        if _WHEEL.match(name):
            wheel = (name, url)
        elif name == SUMS_NAME:
            sums = url
    return (wheel[0], wheel[1], sums) if wheel and sums else None


def expected_hash(sums_text: str, name: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
            return parts[0].lower()
    return None


def _fetch(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "VeinServerManager"})
    with urllib.request.urlopen(request, timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise VsmError("The download is larger than expected.")
    return data


# ---------- the update ----------

class SelfUpdate:
    def __init__(self, manager):
        self.m = manager
        self.folder = self.m.paths.data / "manager"
        self.state = "idle"  # idle / working / restarting / failed
        self.step = ""
        self.error: str | None = None
        self.rolled_back: str | None = None  # a version that was installed but did not start
        self._lock = threading.Lock()

    @property
    def supported(self) -> bool:
        return SUPPORTED and self.m.restart_hook is not None

    def status(self) -> dict:
        return {"supported": self.supported, "state": self.state, "step": self.step, "error": self.error, "rolled_back": self.rolled_back}

    def start(self) -> str:
        """Begin updating in the background; returns the version being installed."""
        if not self.supported:
            raise VsmError("This manager cannot update itself. On Windows use Setup; otherwise re-run the install script.", 409)
        info = self.m.updates.check(force=True)
        target = assets(self.m.updates.release) if info["available"] else None
        if not info["available"]:
            raise VsmError("The manager is already up to date.", 409)
        if not target:
            raise VsmError(f"Release {info['latest']} has no Linux package to install from. Re-run the install script instead.", 409)
        with self._lock:
            if self.state in ("working", "restarting"):
                raise VsmError("An update is already running.", 409)
            self.state, self.step, self.error = "working", "Starting", None
        threading.Thread(target=self._run, args=(str(info["latest"]), *target), name="self-update", daemon=True).start()
        return str(info["latest"])

    def _progress(self, step: str) -> None:
        self.step = step
        self.m.event("UPDATE", f"Manager update: {step}.")
        self.m.server.publish_status()

    def _run(self, tag: str, name: str, url: str, sums_url: str) -> None:
        try:
            python = self._install(tag, name, url, sums_url)
            self.state = "restarting"
            self._progress(f"restarting into {tag}; the game server keeps running")
            self.m.reexec = python
            self.m.restart_hook()
        except Exception as e:  # noqa: BLE001 - reported to the person who asked
            self.state, self.error = "failed", str(e) if isinstance(e, VsmError) else f"{type(e).__name__}: {e}"
            self.m.event("UPDATE", f"Manager update failed: {self.error}")
            log.exception("self-update failed")
            self.m.server.publish_status()

    def _install(self, tag: str, name: str, url: str, sums_url: str) -> str:
        """Download, verify and install the release into its own virtualenv; returns its python."""
        self._progress(f"downloading {tag}")
        wheel = _fetch(url, MAX_WHEEL)
        wanted = expected_hash(_fetch(sums_url, 64 * 1024).decode("utf-8", "replace"), name)
        if not wanted or hashlib.sha256(wheel).hexdigest() != wanted:
            raise VsmError("The download does not match the release's checksum; nothing was installed.")

        target = self.folder / "versions" / tag.lstrip("v")
        shutil.rmtree(target, ignore_errors=True)  # left by an earlier attempt
        target.parent.mkdir(parents=True, exist_ok=True)
        package = self.folder / name
        package.write_bytes(wheel)
        python = str(target / "bin" / "python")
        try:
            self._progress("installing")
            self._call([sys.executable, "-m", "venv", str(target)])
            self._call([python, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--quiet", f"{package}[discord]"])
            got = self._call([python, "-c", "import vsm; print(vsm.__version__)"]).strip()
            if parse(got) is None or parse(got) != parse(tag):
                raise VsmError(f"The installed package says it is version {got or 'unknown'}, not {tag}.")
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        finally:
            package.unlink(missing_ok=True)

        state = read_state(self.folder)
        running = state.get("current") if isinstance(state.get("current"), dict) and state["current"].get("version") == __version__ else None
        write_state(self.folder, {"current": {"version": got, "python": python, "tried": True, "healthy": False}, "previous": running, "failed": None})
        return python

    def _call(self, command: list[str]) -> str:
        done = subprocess.run(command, capture_output=True, text=True, timeout=PIP_TIMEOUT, stdin=subprocess.DEVNULL, cwd=str(self.folder))
        if done.returncode != 0:
            tail = (done.stderr or done.stdout or "").strip().splitlines()[-3:]
            raise VsmError(f"{Path(command[0]).name} {' '.join(command[1:3])} failed: {' | '.join(tail) or 'no output'}")
        return done.stdout

    # ---------- after a start ----------

    def settle(self) -> None:
        """Run once this process has stayed up: trust the running version and tidy up."""
        state = read_state(self.folder)
        if not state:
            return
        current = state.get("current") if isinstance(state.get("current"), dict) else None
        if current and current.get("version") == __version__ and not current.get("healthy"):
            current["healthy"] = True
            write_state(self.folder, state)
            self.m.event("UPDATE", f"The manager was updated to {__version__}.")
        failed = state.get("failed")
        if isinstance(failed, dict) and failed.get("version"):
            self.rolled_back = str(failed["version"])
            self.m.event("UPDATE", f"The manager update to {failed['version']} did not start, so the previous version is running.")
        # Folders are named after the release tag, entries after the package version: compare paths.
        keep = {Path(str(e.get("python"))).parent.parent for e in (state.get("current"), state.get("previous")) if isinstance(e, dict)}
        for folder in (self.folder / "versions").glob("*"):
            if folder not in keep:
                shutil.rmtree(folder, ignore_errors=True)


def attach(manager, server) -> None:
    """`python -m vsm` only: let the manager restart itself in place, and settle after a while."""
    if not SUPPORTED:
        return
    def restart() -> None:
        manager.stop_game_on_exit = False  # the game server outlives this process image
        manager.restart_requested = True
        server.should_exit = True

    manager.restart_hook = restart
    _restore_sessions(manager)
    timer = threading.Timer(HEALTHY_AFTER, lambda: _quietly(manager.self_update.settle))
    timer.daemon = True
    timer.start()


def _keep_sessions(manager) -> None:
    """Across the in-place restart only: who was signed in, as the hashes the manager already
    holds (a cookie cannot be made from one), in a file only this account can read."""
    try:
        folder = manager.self_update.folder
        folder.mkdir(parents=True, exist_ok=True)
        fd = os.open(folder / SESSIONS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(manager.auth.export_sessions(), f)
    except Exception:  # noqa: BLE001 - people sign in again, nothing worse
        log.exception("could not keep the sign-in sessions")


def _restore_sessions(manager) -> None:
    path = manager.self_update.folder / SESSIONS_FILE
    try:
        if not path.is_file():
            return
        fresh = time.time() - path.stat().st_mtime < SESSIONS_MAX_AGE
        data = json.loads(path.read_text(encoding="utf-8")) if fresh else None
        path.unlink()
        manager.auth.import_sessions(data)
    except Exception:  # noqa: BLE001
        log.exception("could not restore the sign-in sessions")
        path.unlink(missing_ok=True)


def _quietly(action) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001
        log.exception("could not settle after the update")


def reexec(manager) -> None:
    """After the HTTP server has stopped: if a restart was asked for, become the new process."""
    if not SUPPORTED or not manager.restart_requested:
        return
    python = manager.reexec or sys.executable
    _keep_sessions(manager)
    logging.shutdown()
    os.execv(python, [python, "-m", "vsm"])
