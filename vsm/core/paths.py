"""Filesystem layout and the per-platform differences of the Vein server."""
from __future__ import annotations

import os
import re
import zipfile
from pathlib import Path

from .. import VsmError

IS_WINDOWS = os.name == "nt"
VEIN_APP_ID = "2131400"

_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_name(name: str) -> str:
    """Validate a user-supplied file or folder name (no traversal, no separators)."""
    if (
        not name
        or len(name) > 128
        or name != name.strip()
        or name.startswith(".")
        or ".." in name
        or _BAD_NAME.search(name)
    ):
        raise VsmError("Invalid name.")
    return name


def child(directory: Path, name: str) -> Path:
    """Resolve `name` directly inside `directory`, refusing anything that escapes it."""
    path = (directory / safe_name(name)).resolve()
    if path.parent != directory.resolve():
        raise VsmError("Invalid name.")
    return path


def extract_zip(zf: zipfile.ZipFile, members: list[tuple[zipfile.ZipInfo, str]], dest: Path,
                max_bytes: int | None = None, max_files: int | None = None) -> None:
    """Extract `members` (zip entry, path relative to `dest`) without trusting the archive.

    Every target must stay inside `dest`, and the limits are enforced on the bytes
    actually written, not on the sizes the archive claims.
    """
    dest = dest.resolve()
    files = [(info, rel) for info, rel in members if not info.is_dir()]
    if max_files is not None and len(files) > max_files:
        raise VsmError("The archive contains too many files.")
    targets = []
    for info, rel in files:
        target = (dest / rel).resolve()
        if not rel or not target.is_relative_to(dest) or target == dest:
            raise VsmError("The archive contains unsafe paths; refusing to extract it.")
        targets.append((info, target))
    written = 0
    for info, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            while chunk := src.read(1024 * 1024):
                written += len(chunk)
                if max_bytes is not None and written > max_bytes:
                    raise VsmError("The archive is too large when unpacked.")
                out.write(chunk)


class Paths:
    def __init__(self) -> None:
        self.data = Path(os.environ.get("VSM_DATA_DIR") or "data").resolve()
        self.server = Path(os.environ.get("VSM_SERVER_DIR") or self.data / "server").resolve()
        self.config_file = self.data / "config.json"
        self.auth_file = self.data / "auth.json"
        self.history_file = self.data / "player_history.json"
        self.profiles = self.data / "profiles"
        self.backups = self.data / "backups"
        self.logs = self.data / "logs"
        self.steamcmd = self.data / "steamcmd"

        self.saved = self.server / "Vein" / "Saved"
        self.game_log = self.saved / "Logs" / "Vein.log"
        self.mods = self.server / "Vein" / "Content" / "Paks" / "~mods"
        self.manifest = self.server / "steamapps" / f"appmanifest_{VEIN_APP_ID}.acf"
        if IS_WINDOWS:
            self.binaries = self.server / "Vein" / "Binaries" / "Win64"
            self.server_exe = self.binaries / "VeinServer-Win64-Test.exe"
            self.ini_dir = self.saved / "Config" / "WindowsServer"
        else:
            self.binaries = self.server / "Vein" / "Binaries" / "Linux"
            self.server_exe = self.binaries / "VeinServer-Linux-Test"
            self.ini_dir = self.saved / "Config" / "LinuxServer"
        # UE4SS is injected through a proxy DLL beside the Windows server executable. The
        # paths are defined on every platform so the file handling can be tested anywhere.
        self.ue4ss_root = self.server / "Vein" / "Binaries" / "Win64"
        self.ue4ss_dir = self.ue4ss_root / "ue4ss"
        self.ue4ss_proxy = self.ue4ss_root / "dwmapi.dll"
        self.logic_mods = self.server / "Vein" / "Content" / "Paks" / "LogicMods"
        self.ue4ss_state = self.data / "ue4ss.json"
        self.game_ini = self.ini_dir / "Game.ini"
        self.engine_ini = self.ini_dir / "Engine.ini"

    def ensure(self) -> None:
        for d in (self.data, self.server, self.profiles, self.backups, self.logs):
            d.mkdir(parents=True, exist_ok=True)
