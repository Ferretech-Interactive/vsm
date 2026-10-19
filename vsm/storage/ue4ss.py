"""UE4SS (script mod loader) management for the Windows server build.

UE4SS is injected by a proxy `dwmapi.dll` placed beside the server executable and
keeps everything else in an `ue4ss/` folder next to it:

    Vein/Binaries/Win64/dwmapi.dll            proxy; renaming it away disables UE4SS
    Vein/Binaries/Win64/ue4ss/UE4SS.dll
    Vein/Binaries/Win64/ue4ss/UE4SS-settings.ini
    Vein/Binaries/Win64/ue4ss/Mods/mods.txt   load order and enabled state
    Vein/Binaries/Win64/ue4ss/Mods/<Name>/Scripts/main.lua   (Lua mod)
    Vein/Binaries/Win64/ue4ss/Mods/<Name>/dlls/main.dll      (C++ mod)
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .. import VsmError
from ..core.paths import IS_WINDOWS, child, extract_zip, safe_name

RELEASE_API = "https://api.github.com/repos/UE4SS-RE/RE-UE4SS/releases/tags/experimental-latest"
DOWNLOAD_PREFIX = "https://github.com/UE4SS-RE/RE-UE4SS/releases/download/"
ASSET = re.compile(r"^UE4SS_(v[\w.\-]+)\.zip$")  # the end-user build, not zDEV-*
# UE4SS publishes per-game configs beside each build. Vein's engine differs from stock Unreal
# (object layouts, signatures, engine version), and without its config the server crashes
# the moment the engine finishes loading.
CONFIGS_ASSET = "zCustomGameConfigs.zip"
GAME_CONFIG = "Vein/"
LAYOUT_FILE = "MemberVariableLayout.ini"  # present once a game config has been applied
ENGINE_OVERRIDE_KEYS = ("MajorVersion", "MinorVersion", "DebugBuild")

MAX_FRAMEWORK_BYTES = 300 * 1024**2
MAX_MOD_BYTES = 1024**3
MAX_FILES = 5000
LOG_TAIL_BYTES = 64 * 1024

DISABLED = ".disabled"
KEYBINDS = "Keybinds"  # UE4SS requires this entry to stay last in mods.txt
# Shipped with UE4SS. Only the blueprint loader is useful on a headless dedicated server.
BUILTIN = {"CheatManagerEnablerMod", "ConsoleCommandsMod", "ConsoleEnablerMod", "SplitScreenMod",
           "LineTraceMod", "BPML_GenericFunctions", "BPModLoaderMod", "jsbLuaProfilerMod", "ActorDumperMod", KEYBINDS}
SERVER_BUILTIN = {"BPML_GenericFunctions", "BPModLoaderMod"}
# No console windows, and no crash-dump dialog: a modal box on a machine with nobody at the
# desktop would hold the crashed server open forever instead of letting the watchdog restart it.
HEADLESS_SETTINGS = {"ConsoleEnabled": "0", "GuiConsoleEnabled": "0", "GuiConsoleVisible": "0", "EnableDumping": "0"}
KEEP_ON_UPDATE = ("ue4ss/UE4SS-settings.ini", "ue4ss/Mods/mods.txt")


class Ue4ss:
    def __init__(self, manager):
        self.m = manager
        self.supported = IS_WINDOWS
        self.root: Path = manager.paths.ue4ss_root
        self.dir: Path = manager.paths.ue4ss_dir
        self.proxy: Path = manager.paths.ue4ss_proxy
        self.mods_dir: Path = self.dir / "Mods"
        self.mods_txt: Path = self.mods_dir / "mods.txt"
        self.state_file: Path = manager.paths.ue4ss_state

    def require_supported(self) -> None:
        if not self.supported:
            raise VsmError("UE4SS only works with the Windows server build.", 409)

    # ---------- status ----------

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    @property
    def installed(self) -> bool:
        return (self.dir / "UE4SS.dll").is_file()

    @property
    def enabled(self) -> bool:
        return self.installed and self.proxy.is_file()

    def build_changed(self) -> bool:
        """True when the game was updated after UE4SS was installed (updates commonly break it)."""
        recorded = self._state().get("game_build")
        current = self.m.steam.build_id()
        return bool(self.installed and recorded and current and recorded != current)

    def status(self) -> dict:
        state = self._state() if self.installed else {}
        return {
            "supported": self.supported,
            "installed": self.installed,
            "enabled": self.enabled,
            "version": state.get("version"),
            "source": state.get("source"),
            "installed_at": state.get("installed_at"),
            "game_build": state.get("game_build"),
            "build_changed": self.build_changed(),
            "game_config": (self.dir / LAYOUT_FILE).is_file(),
            "mods": self.mods() if self.installed else [],
        }

    # ---------- framework install ----------

    def _release_assets(self) -> dict[str, str]:
        """Asset name -> download URL for the current UE4SS experimental release."""
        request = urllib.request.Request(RELEASE_API, headers={"User-Agent": "VeinServerManager", "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(request, timeout=20) as r:
                assets = json.loads(r.read().decode())["assets"]
        except Exception as e:
            raise VsmError(f"Could not reach GitHub to find the UE4SS release: {e}", 502)
        return {a["name"]: a["browser_download_url"] for a in assets if a["browser_download_url"].startswith(DOWNLOAD_PREFIX)}

    def _download(self, url: str):
        """Fetch a release asset into a temporary file (caller closes it)."""
        tmp = tempfile.TemporaryFile()
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                self._spool(r, tmp, MAX_FRAMEWORK_BYTES)
        except VsmError:
            tmp.close()
            raise
        except Exception as e:
            tmp.close()
            raise VsmError(f"Downloading {url.rsplit('/', 1)[-1]} failed: {e}", 502)
        return tmp

    def install_from_github(self) -> dict:
        self.require_supported()
        assets = self._release_assets()
        name = next((n for n in assets if ASSET.match(n)), None)
        if not name:
            raise VsmError("No UE4SS build was found in the experimental release.", 502)
        with self._download(assets[name]) as tmp:
            return self._install(tmp, source="github", asset=name)

    def install_from_upload(self, filename: str, stream) -> dict:
        self.require_supported()
        with tempfile.TemporaryFile() as tmp:
            self._spool(stream, tmp, MAX_FRAMEWORK_BYTES)
            return self._install(tmp, source="upload", asset=os.path.basename(filename or "upload.zip"))

    @staticmethod
    def _spool(src, dst, limit: int) -> None:
        total = 0
        while chunk := src.read(1024 * 1024):
            total += len(chunk)
            if total > limit:
                raise VsmError("File is too large.", 413)
            dst.write(chunk)
        dst.seek(0)

    def _install(self, archive, source: str, asset: str) -> dict:
        if not self.root.is_dir():
            raise VsmError("Install the game server first.")
        digest = hashlib.sha256(archive.read()).hexdigest()
        archive.seek(0)
        try:
            zf = zipfile.ZipFile(archive)
        except zipfile.BadZipFile:
            raise VsmError("That file is not a zip archive.")
        with zf:
            names = zf.namelist()
            # The files may sit at the top of the zip or under any folder (repacks often nest them).
            prefix = next((n[: -len("ue4ss/UE4SS.dll")] for n in names if n.lower().endswith("ue4ss/ue4ss.dll")), None)
            if prefix is None or f"{prefix}dwmapi.dll".lower() not in {n.lower() for n in names}:
                raise VsmError("This zip is not a UE4SS build with the ue4ss/ folder layout (use the experimental release).")
            # A package made for Vein already carries the game config; anything else needs it
            # fetched, and that must succeed before any file is replaced.
            bundled_config = f"{prefix}ue4ss/{LAYOUT_FILE}".lower() in {n.lower() for n in names}
            config = None if bundled_config and source == "upload" else self._download_game_config()
            was_disabled = self.installed and not self.proxy.is_file()
            members = []
            for info in zf.infolist():
                if not info.filename.startswith(prefix):
                    continue
                rel = info.filename[len(prefix):]
                lower = rel.lower()
                if lower == "dwmapi.dll":
                    rel = self.proxy.name + (DISABLED if was_disabled else "")
                elif not lower.startswith("ue4ss/"):
                    continue
                elif lower in (k.lower() for k in KEEP_ON_UPDATE) and (self.root / rel).exists():
                    continue  # keep the user's settings and load order across updates
                members.append((info, rel))
            extract_zip(zf, members, self.root, MAX_FRAMEWORK_BYTES, MAX_FILES)

        if not was_disabled:
            (self.root / (self.proxy.name + DISABLED)).unlink(missing_ok=True)
        if config:
            with config:
                self._apply_game_config(config)
        self._set_settings(HEADLESS_SETTINGS)
        if not self._state():  # first install: switch off the client-only built-in mods
            self.set_mods([{"name": mod["name"], "enabled": mod["name"] in SERVER_BUILTIN or not mod["builtin"] and mod["enabled"]}
                           for mod in self.mods()])
        match = ASSET.match(asset)
        state = {
            "version": match.group(1) if match else asset,
            "source": source,
            "asset": asset,
            "sha256": digest,
            "installed_at": time.time(),
            "game_build": self.m.steam.build_id(),
        }
        self.state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        self.m.event("UE4SS", f"Installed UE4SS {state['version']} ({source}).")
        return self.status()

    def _download_game_config(self):
        assets = self._release_assets()
        if CONFIGS_ASSET not in assets:
            raise VsmError("The UE4SS release has no game configs; cannot set UE4SS up for Vein.", 502)
        return self._download(assets[CONFIGS_ASSET])

    def _apply_game_config(self, archive) -> None:
        """Copy Vein's layout and signature files in, and take its engine-version override."""
        with zipfile.ZipFile(archive) as zf:
            members = [(i, i.filename[len(GAME_CONFIG):]) for i in zf.infolist()
                       if i.filename.startswith(GAME_CONFIG) and not i.filename.endswith("UE4SS-settings.ini")]
            if not any(rel == LAYOUT_FILE for _, rel in members):
                raise VsmError("The UE4SS release has no config for Vein; cannot set UE4SS up for this game.", 502)
            extract_zip(zf, members, self.dir, MAX_FRAMEWORK_BYTES, MAX_FILES)
            # Their settings file is older than the build's, so only the engine override is taken from it.
            theirs = zf.read(GAME_CONFIG + "UE4SS-settings.ini").decode("utf-8", errors="replace")
        override = {}
        for key in ENGINE_OVERRIDE_KEYS:
            match = re.search(rf"(?mi)^{key}[ \t]*=[ \t]*(\S*)", theirs)
            if match and match.group(1):
                override[key] = match.group(1)
        self._set_settings(override)

    def _set_settings(self, values: dict[str, str]) -> None:
        """Set keys in UE4SS-settings.ini in place, keeping its comments and `Key = Value` style."""
        path = self.dir / "UE4SS-settings.ini"
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for key, value in values.items():
            value = str(value)
            if not re.fullmatch(r"[\w.]+", value):  # also comes from the downloaded game config
                raise VsmError(f"Unexpected value for {key} in the UE4SS settings.")
            text = re.sub(rf"(?mi)^({re.escape(key)}[ \t]*=)[^\r\n]*$", lambda m, v=value: f"{m.group(1)} {v}", text)
        path.write_text(text, encoding="utf-8")

    def set_enabled(self, enabled: bool) -> dict:
        """Safe mode: with the proxy DLL renamed away the server starts without UE4SS or any script mod."""
        self.require_supported()
        if not self.installed:
            raise VsmError("UE4SS is not installed.")
        parked = self.proxy.with_name(self.proxy.name + DISABLED)
        if enabled and parked.is_file():
            os.replace(parked, self.proxy)
        elif not enabled and self.proxy.is_file():
            os.replace(self.proxy, parked)
        self.m.event("UE4SS", "UE4SS enabled." if enabled else "UE4SS disabled (safe mode).")
        return self.status()

    def remove(self) -> dict:
        """Remove the framework but keep the user's script mods and load order."""
        self.require_supported()
        for p in (self.proxy, self.proxy.with_name(self.proxy.name + DISABLED)):
            p.unlink(missing_ok=True)
        if self.dir.is_dir():
            for entry in self.dir.iterdir():
                if entry.name == "Mods":
                    continue
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        self.state_file.unlink(missing_ok=True)
        self.m.event("UE4SS", "Removed UE4SS.")
        return self.status()

    # ---------- script mods ----------

    def _read_mods_txt(self) -> list[tuple[str, bool]]:
        try:
            lines = self.mods_txt.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            line = line.strip().lstrip("﻿")
            if not line or line.startswith(";") or ":" not in line:
                continue
            name, _, flag = line.rpartition(":")
            out.append((name.strip(), flag.strip() == "1"))
        return out

    @staticmethod
    def _kind(folder: Path) -> str:
        if (folder / "dlls" / "main.dll").is_file():
            return "cpp"
        if (folder / "Scripts" / "main.lua").is_file():
            return "lua"
        return "unknown"

    def mods(self) -> list[dict]:
        """Mods in load order: mods.txt first, then folders it does not mention."""
        listed = self._read_mods_txt()
        folders = {p.name: p for p in self.mods_dir.iterdir() if p.is_dir()} if self.mods_dir.is_dir() else {}
        order = [(name, on) for name, on in listed if name in folders or name == KEYBINDS]
        known = {name for name, _ in order}
        # A folder with enabled.txt loads even when mods.txt does not list it.
        # (Folders that are not mods, such as UE4SS's `shared` library, are left out.)
        order += [(name, (folder / "enabled.txt").exists()) for name, folder in sorted(folders.items())
                  if name not in known and self._kind(folder) != "unknown"]
        out = []
        for name, on in order:
            folder = folders.get(name)
            size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file()) if folder else 0
            out.append({
                "name": name,
                "enabled": on,
                "kind": self._kind(folder) if folder else "builtin",
                "builtin": name in BUILTIN,
                "size": size,
            })
        return out

    def set_mods(self, mods: list[dict]) -> list[dict]:
        """Write the load order and enabled flags. Unknown names are ignored; unmentioned mods are appended."""
        self.require_supported()
        current = {mod["name"]: mod for mod in self.mods()}
        ordered: list[tuple[str, bool]] = []
        for mod in mods:
            name = str(mod.get("name", ""))
            if name in current and name not in {n for n, _ in ordered}:
                ordered.append((name, bool(mod.get("enabled"))))
        ordered += [(name, mod["enabled"]) for name, mod in current.items() if name not in {n for n, _ in ordered}]

        keybinds = next((on for name, on in ordered if name == KEYBINDS), None)
        lines = [f"{name} : {int(on)}" for name, on in ordered if name != KEYBINDS]
        if keybinds is not None:
            lines += ["", "; Built-in keybinds, do not move up!", f"{KEYBINDS} : {int(keybinds)}"]
        self.mods_dir.mkdir(parents=True, exist_ok=True)
        self.mods_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
        # mods.txt is now authoritative; a stray enabled.txt would silently override it.
        for name in current:
            (self.mods_dir / name / "enabled.txt").unlink(missing_ok=True)
        return self.mods()

    def upload_mod(self, filename: str, stream) -> list[str]:
        """Install every UE4SS mod found in a zip. Returns the mod names."""
        self.require_supported()
        if not self.installed:
            raise VsmError("Install UE4SS first.")
        with tempfile.TemporaryFile() as tmp:
            self._spool(stream, tmp, MAX_MOD_BYTES)
            try:
                zf = zipfile.ZipFile(tmp)
            except zipfile.BadZipFile:
                raise VsmError("Script mods are uploaded as a .zip archive.")
            with zf:
                # A mod is any folder holding Scripts/main.lua or dlls/main.dll, however deep the zip nests it.
                roots: dict[str, str] = {}
                for name in zf.namelist():
                    lower = name.lower()
                    for marker in ("scripts/main.lua", "dlls/main.dll"):
                        if lower.endswith(marker) and (len(lower) == len(marker) or lower[-len(marker) - 1] == "/"):
                            root = name[: -len(marker)]
                            mod_name = PurePosixPath(root).name or Path(os.path.basename(filename or "mod")).stem
                            roots[root] = safe_name(mod_name)
                if not roots:
                    raise VsmError("No UE4SS mod found in this zip (expected Scripts/main.lua or dlls/main.dll).")
                installed = []
                for root, mod_name in roots.items():
                    if mod_name in BUILTIN:
                        raise VsmError(f"'{mod_name}' is a built-in UE4SS mod and cannot be replaced.")
                    target = child(self.mods_dir, mod_name)
                    members = [(i, i.filename[len(root):]) for i in zf.infolist() if i.filename.startswith(root) and i.filename != root]
                    if target.exists():
                        shutil.rmtree(target)
                    try:
                        extract_zip(zf, members, target, MAX_MOD_BYTES, MAX_FILES)
                    except VsmError:
                        shutil.rmtree(target, ignore_errors=True)
                        raise
                    installed.append(mod_name)
        listed = {name for name, _ in self._read_mods_txt()}
        current = self.mods()
        # New mods load after existing ones and start enabled.
        self.set_mods([{**mod, "enabled": True} if mod["name"] in installed and mod["name"] not in listed else mod for mod in current])
        self.m.event("UE4SS", f"Installed script mod(s): {', '.join(installed)}.")
        return installed

    def delete_mod(self, name: str) -> list[dict]:
        self.require_supported()
        if name in BUILTIN:
            raise VsmError("Built-in UE4SS mods cannot be deleted; disable them instead.")
        target = child(self.mods_dir, name)
        if not target.is_dir():
            raise VsmError("Mod not found.", 404)
        shutil.rmtree(target)
        self.m.event("UE4SS", f"Deleted script mod {name}.")
        return self.set_mods(self.mods())

    def log_tail(self) -> list[str]:
        path = self.dir / "UE4SS.log"
        try:
            with open(path, "rb") as f:
                f.seek(max(0, path.stat().st_size - LOG_TAIL_BYTES))
                return f.read().decode("utf-8", errors="replace").splitlines()[-400:]
        except OSError:
            return []
