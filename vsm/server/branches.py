"""The official list of Steam branches for the Vein server, read with SteamCMD and remembered.

SteamCMD runs as its own process on a background thread, so asking for the list never blocks:
callers get what is stored, plus `loading` while a refresh is under way.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import threading
import time

from .. import VsmError
from ..core.network import clean_dll_search
from ..core.paths import IS_WINDOWS, VEIN_APP_ID

log = logging.getLogger("vsm.branches")

MAX_AGE = 6 * 60 * 60  # refresh a stored list older than this when someone looks at it
RETRY_AFTER = 5 * 60  # after a failed attempt, do not retry automatically for this long
TIMEOUT = 180
MAX_OUTPUT = 4 * 1024 * 1024
MAX_BRANCHES = 300
MAX_DEPTH = 32
NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")  # the same rule as behavior.steam_branch
_ESCAPE = re.compile(r"\\(.)")
_TOKEN = re.compile(r'"((?:[^"\\]|\\.)*)"|([{}])')


def parse_vdf(text: str) -> dict:
    """Parse Valve's key/value text: quoted keys followed by a quoted value or a { block }."""
    root: dict = {}
    stack = [root]
    key = None
    for match in _TOKEN.finditer(text):
        string, brace = match.groups()
        if brace == "{":
            if key is None or len(stack) >= MAX_DEPTH:
                raise ValueError("unexpected {")
            child: dict = {}
            stack[-1][key] = child
            stack.append(child)
            key = None
        elif brace == "}":
            if key is not None or len(stack) == 1:
                raise ValueError("unexpected }")
            stack.pop()
            if len(stack) == 1:
                break  # the first top-level block is the app; anything after it is SteamCMD chatter
        elif key is None:
            key = string
        else:
            stack[-1][key] = _ESCAPE.sub(r"\1", string)
            key = None
    return root


def _number(value) -> int:
    return int(value) if isinstance(value, str) and value.isdigit() and len(value) < 16 else 0


def parse_branches(output: str, app_id: str = VEIN_APP_ID) -> list[dict]:
    """Pick the branch list out of `app_info_print` output. Returns [] when it is not there."""
    start = re.search(rf'^"{re.escape(app_id)}"\s*$', output, re.MULTILINE)
    if not start:
        return []
    try:
        branches = parse_vdf(output[start.start():])[app_id]["depots"]["branches"]
    except (ValueError, KeyError, TypeError):
        return []
    if not isinstance(branches, dict):
        return []
    found = []
    for name, info in branches.items():
        if not NAME.match(name) or not isinstance(info, dict):
            continue
        description = info.get("description")
        found.append({
            "name": name,
            "description": description[:200] if isinstance(description, str) else "",
            "build_id": _number(info.get("buildid")),
            "updated": _number(info.get("timeupdated")),
            "password_required": info.get("pwdrequired") == "1",
        })
    found.sort(key=lambda b: b["build_id"], reverse=True)
    return found[:MAX_BRANCHES]


class Branches:
    def __init__(self, manager):
        self.m = manager
        self.file = manager.paths.data / "branches.json"
        self._lock = threading.Lock()
        self._loading = False
        self._attempted = 0.0
        self._error = ""
        self._proc: subprocess.Popen | None = None
        self._cancelled = False
        self._data = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            return {"checked_at": float(data["checked_at"]), "branches": [b for b in data["branches"] if NAME.match(b["name"])]}
        except (OSError, ValueError, KeyError, TypeError):
            return {"checked_at": 0, "branches": []}

    def status(self) -> dict:
        return {"loading": self._loading, "error": self._error, **self._data}

    def refresh(self) -> bool:
        """Start a refresh in the background. False if one is already running."""
        with self._lock:
            if self._loading:
                return False
            self._loading = True
            self._cancelled = False
            self._attempted = time.time()
        threading.Thread(target=self._work, name="branches", daemon=True).start()
        return True

    def refresh_if_stale(self) -> None:
        now = time.time()
        if now - self._data["checked_at"] > MAX_AGE and now - self._attempted > RETRY_AFTER:
            self.refresh()

    def cancel(self) -> None:
        """Stop a refresh in progress (an install or update needs SteamCMD); it is not retried."""
        self._cancelled = True
        proc = self._proc
        if proc:
            proc.kill()

    def _work(self) -> None:
        error = ""
        try:
            data = {"checked_at": time.time(), "branches": self._fetch()}
            temp = self.file.with_suffix(".tmp")
            temp.write_text(json.dumps(data), encoding="utf-8")
            temp.replace(self.file)
            self._data = data
        except VsmError as e:
            error = str(e)
        except Exception as e:
            log.warning("reading the Steam branch list failed: %s", e)
            error = f"Could not read the branch list: {e}"
        finally:
            self._error = error
            self._loading = False

    def _fetch(self) -> list[dict]:
        steam = self.m.steam
        # SteamCMD cannot run twice from one folder: an install or update has priority.
        if not steam.lock.acquire(blocking=False):
            raise VsmError("SteamCMD is busy installing or updating the server. Try again when it has finished.")
        try:
            steam.ensure()
            # A cached copy would hide new builds, and app_info_update alone does not always replace it.
            (steam.exe.parent / "appcache" / "appinfo.vdf").unlink(missing_ok=True)
            cmd = [str(steam.exe), "+login", "anonymous", "+app_info_update", "1", "+app_info_print", VEIN_APP_ID, "+quit"]
            # SteamCMD's first run only updates itself and prints nothing useful; one retry covers it.
            for _attempt in (1, 2):
                if self._cancelled:
                    break
                branches = parse_branches(self._run(cmd))
                if branches:
                    return branches
            if self._cancelled:
                raise VsmError("The branch list was not refreshed because an install or update started. Try again when it has finished.")
            raise VsmError("Steam did not return a branch list. Check the internet connection and try again.")
        finally:
            steam.lock.release()

    def _run(self, cmd: list[str]) -> str:
        flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}
        with clean_dll_search():
            self._proc = proc = subprocess.Popen(
                cmd, cwd=self.m.steam.exe.parent, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, errors="replace", **flags,
            )
        try:
            return proc.communicate(timeout=TIMEOUT)[0][:MAX_OUTPUT]
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise VsmError("SteamCMD took too long to answer. Try again.")
        finally:
            self._proc = None
