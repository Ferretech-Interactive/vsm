"""How this copy of the desktop app is set up.

`desktop.json` sits beside the executable (in Program Files only administrators can change
it) and decides the mode:

  service   installed by Setup as a host: the manager runs as a Windows service and this
            program is only a window onto it
  client    installed by Setup to manage servers on other machines; nothing runs locally
  portable  no installer (the zip, or running from source): the manager runs inside this
            program, as whoever started it

Environment variables still win over the file, so a build can be pointed elsewhere for testing.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
APP_DIR = Path(sys.executable).parent if FROZEN else Path.cwd()
SETTINGS_FILE = APP_DIR / "desktop.json"
MODES = ("service", "client", "portable")


def load() -> dict:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def mode() -> str:
    value = load().get("mode")
    return value if value in MODES else "portable"


def apply(as_service: bool = False) -> None:
    """Export settings as the VSM_* variables the manager already understands."""
    settings = load()
    default_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VeinServerManager" if FROZEN else None
    data = settings.get("data_dir")
    if data and not Path(data).is_absolute():
        data = APP_DIR / data  # the portable build keeps its data beside the executable
    for env, value in (
        ("VSM_DATA_DIR", data or default_data),
        ("VSM_HOST", settings.get("host")),
        ("VSM_PORT", settings.get("port")),
    ):
        if value and not os.environ.get(env):
            os.environ[env] = str(value)
    if as_service:
        os.environ["VSM_LOCAL_LOGIN"] = "pipe"  # administrators sign in through the named pipe
        # A service account's own profile and temp folders are not somewhere to rely on.
        temp = data_dir() / "tmp"
        temp.mkdir(parents=True, exist_ok=True)
        os.environ["TEMP"] = os.environ["TMP"] = str(temp)
    elif mode() == "portable":
        os.environ["VSM_LOCAL_LOGIN"] = "1"  # same user runs manager and window: a token file is enough


def port() -> int:
    return int(os.environ.get("VSM_PORT", "8810"))


def data_dir() -> Path:
    return Path(os.environ.get("VSM_DATA_DIR") or "data").resolve()


def client_dir() -> Path:
    """Where the window keeps its own state (log, window size, server list).

    In service and client modes the window cannot, or has no reason to, write to the server's
    data folder, so it uses the Windows user's own profile.
    """
    if mode() == "portable":
        return data_dir()
    path = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "VeinServerManager"
    path.mkdir(parents=True, exist_ok=True)
    return path
