"""Reads and writes the settings the manager owns inside Game.ini / Engine.ini."""
from __future__ import annotations

import os
import re

from .. import VsmError
from ..core.config import ManagerConfig
from .gameplay import SCARCITY_OPTIONS, rows
from .ini import IniFile
from ..core.paths import Paths

GAME_SESSION = "/Script/Vein.VeinGameSession"
SERVER_SETTINGS = "/Script/Vein.ServerSettings"
ENGINE_SESSION = "/Script/Engine.GameSession"
GAME_STATE = "/Script/Vein.VeinGameStateBase"

# Sections the generic ini editor must not touch; they have dedicated UI.
PROTECTED_SECTIONS = {
    GAME_SESSION.lower(),
    SERVER_SETTINGS.lower(),
    ENGINE_SESSION.lower(),
    "/script/engine.gamenetworkmanager",
    "core.system",
    "url",
}

_STEAM_ID = re.compile(r"^\d{17}$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")
_PASSWORD = re.compile(r"^[A-Za-z0-9_\-]{0,64}$")
_DISCORD_WEBHOOK = re.compile(r"^https://((ptb|canary)\.)?discord(app)?\.com/api/webhooks/[A-Za-z0-9/_\-]{1,200}$")
# The game forwards global chat, and admin reports, to these itself.
DISCORD_KEYS = {"discord_chat_webhook": "DiscordChatWebhookURL", "discord_admin_webhook": "DiscordChatAdminWebhookURL"}


def steam_ids(values: list[str]) -> list[str]:
    out = []
    for v in values:
        for part in str(v).split(","):
            part = part.strip()
            if not part:
                continue
            if not _STEAM_ID.match(part):
                raise VsmError(f"'{part}' is not a 17-digit SteamID64.")
            if part not in out:
                out.append(part)
    return out


class GameSettings:
    def __init__(self, paths: Paths):
        self.paths = paths
        self._pending_bans: list[str] | None = None  # changed while the server ran; written again when it stops

    def read(self) -> dict:
        ini = IniFile(self.paths.game_ini)
        name = ini.get(GAME_SESSION, "ServerName") or ini.get(SERVER_SETTINGS, "ServerName") or "Vein Server"
        port = ini.get(GAME_SESSION, "HTTPPort") or ini.get(SERVER_SETTINGS, "HTTPPort") or "0"
        return {
            "server_name": name,
            "description": ini.get(GAME_SESSION, "ServerDescription", ""),
            "password": ini.get(GAME_SESSION, "Password", ""),
            "http_port": int(port) if port.isdigit() else 0,
            "super_admins": _ids(ini, "SuperAdminSteamIDs"),
            "admins": _ids(ini, "AdminSteamIDs"),
            **{field: (ini.get(SERVER_SETTINGS, key) or "").strip().strip('"') for field, key in DISCORD_KEYS.items()},
        }

    @staticmethod
    def validate(data: dict) -> dict:
        """Check and normalise the fields present in `data`; raises VsmError naming the problem."""
        clean: dict = {}
        if "server_name" in data:
            clean["server_name"] = str(data["server_name"]).strip()[:128] or "Vein Server"
        if "description" in data:
            clean["description"] = str(data["description"]).strip()[:512]
        if "password" in data:
            clean["password"] = str(data["password"])
            if not _PASSWORD.match(clean["password"]):
                raise VsmError("Server password may only contain letters, numbers, dashes and underscores.")
        if "http_port" in data:
            try:
                clean["http_port"] = int(data["http_port"] or 0)
            except (TypeError, ValueError):
                raise VsmError("HTTP API port must be a number.")
            if not 0 <= clean["http_port"] <= 65535:
                raise VsmError("HTTP API port must be between 0 and 65535.")
        for key in ("super_admins", "admins"):
            if key in data:
                clean[key] = steam_ids(data[key] if isinstance(data[key], list) else [data[key]])
        for field in DISCORD_KEYS:
            if field in data:
                clean[field] = str(data[field]).strip().strip('"')
                if clean[field] and not _DISCORD_WEBHOOK.match(clean[field]):
                    raise VsmError("A Discord webhook must be a https://discord.com/api/webhooks/... address.")
        return clean

    def write(self, data: dict) -> None:
        data = self.validate(data)
        ini = IniFile(self.paths.game_ini)
        if "server_name" in data:
            # Written to both sections: the engine resets the name if only one has it.
            ini.set(GAME_SESSION, "ServerName", data["server_name"])
            ini.set(SERVER_SETTINGS, "ServerName", data["server_name"])
        if "description" in data:
            ini.set(GAME_SESSION, "ServerDescription", data["description"])
        if "password" in data:
            if data["password"]:
                ini.set(GAME_SESSION, "Password", data["password"])
            else:
                ini.remove(GAME_SESSION, "Password")
        if "http_port" in data:
            ini.remove(SERVER_SETTINGS, "HTTPPort")
            if data["http_port"]:
                ini.set(GAME_SESSION, "HTTPPort", str(data["http_port"]))
            else:
                ini.remove(GAME_SESSION, "HTTPPort")
        if "super_admins" in data:
            ini.set_array(GAME_SESSION, "SuperAdminSteamIDs", data["super_admins"])
        if "admins" in data:
            ini.set_array(GAME_SESSION, "AdminSteamIDs", data["admins"])
        for field, key in DISCORD_KEYS.items():
            if field in data:
                if data[field]:
                    ini.set(SERVER_SETTINGS, key, f'"{data[field]}"')  # Ramjet: the URL must be in quotes
                else:
                    ini.remove(SERVER_SETTINGS, key)
        ini.save()

    def apply_launch(self, cfg: ManagerConfig) -> None:
        """Keep the ini's player cap in sync with the launch settings before each start."""
        ini = IniFile(self.paths.game_ini)
        ini.set(GAME_SESSION, "MaxPlayers", str(cfg.launch.max_players))
        ini.set(ENGINE_SESSION, "MaxPlayers", str(cfg.launch.max_players))
        ini.save()

    def read_gameplay(self) -> dict:
        files = {"Game": IniFile(self.paths.game_ini), "Engine": IniFile(self.paths.engine_ini)}
        out = {}
        for _label, key, _help, type_, file, section, default in rows():
            raw = files[file].get(section, key)
            if raw is None:
                out[key] = default
            elif type_ == "bool":
                out[key] = raw.lower() in ("1", "true")
            elif type_ == "scarcity":
                try:
                    out[key] = f"{float(raw):.1f}"
                except ValueError:
                    out[key] = default
            else:
                out[key] = raw
        return out

    def write_gameplay(self, values: dict) -> None:
        files = {"Game": IniFile(self.paths.game_ini), "Engine": IniFile(self.paths.engine_ini)}
        for label, key, _help, type_, file, section, _default in rows():
            if key not in values:
                continue
            value = values[key]
            if type_ == "bool":
                text = "1" if value else "0"  # the form Ramjet documents for both ini files
            elif type_ == "scarcity":
                text = str(value)
                if text not in {v for v, _ in SCARCITY_OPTIONS}:
                    raise VsmError(f"{label}: unknown option.")
            else:
                text = str(value).strip()
                if not _NUMBER.match(text):
                    raise VsmError(f"{label}: '{text}' is not a number.")
            files[file].set(section, key, text)
        for f in files.values():
            f.save()

    # --- generic section editor (mods) ---

    def editable_sections(self) -> list[str]:
        return sorted(s for s in IniFile(self.paths.game_ini).sections() if s.lower() not in PROTECTED_SECTIONS)

    def read_section(self, section: str) -> list[tuple[str, str]]:
        return IniFile(self.paths.game_ini).entries(section)

    def write_section(self, section: str, entries: list[tuple[str, str]]) -> None:
        section = section.strip()
        if not section or any(c in section for c in "[]\r\n") or section.lower() in PROTECTED_SECTIONS:
            raise VsmError("This section cannot be edited here.")
        ini = IniFile(self.paths.game_ini)
        ini.replace_section(section, [(k.strip(), v.strip()) for k, v in entries if k.strip()])
        ini.save()

    # --- bans ---

    def bans(self) -> list[str]:
        if self._pending_bans is not None:
            return list(self._pending_bans)
        return IniFile(self.paths.game_ini).get_array(GAME_STATE, "BannedPlayers")

    def set_bans(self, ids: list[str], running: bool = False) -> None:
        """`running`: the server may rewrite Game.ini when it exits, so the list is written again then."""
        ids = steam_ids(ids)
        self._write_bans(ids)
        self._pending_bans = ids if running else None

    def flush_bans(self) -> None:
        """Called once the server has stopped, and before it starts: re-apply a list changed while it ran."""
        if self._pending_bans is not None:
            self._write_bans(self._pending_bans)
            self._pending_bans = None

    def _write_bans(self, ids: list[str]) -> None:
        ini = IniFile(self.paths.game_ini)
        ini.set_array(GAME_STATE, "BannedPlayers", ids)
        ini.save()

    def installed(self) -> bool:
        return os.path.exists(self.paths.server_exe)


def _ids(ini: IniFile, key: str) -> list[str]:
    out = []
    for value in ini.get_array(GAME_SESSION, key):
        out += [p.strip() for p in value.split(",") if p.strip()]
    return out
