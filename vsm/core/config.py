"""Manager configuration: a validated JSON document in the data directory.

Environment variables (see ENV_OVERRIDES) are applied on every start so a
container can be configured without ever opening the UI.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError, field_validator

from .. import VsmError

MASK = "********"
SECRET_FIELDS = (("rcon", "password"), ("discord", "bot_token"), ("discord", "webhook_url"))
_WEBHOOK = re.compile(r"^https://((ptb|canary)\.)?discord(app)?\.com/api/webhooks/\S+$")


class LaunchConfig(BaseModel):
    map: str = Field("/Game/Vein/Maps/ChamplainValley?listen", pattern=r"^/[A-Za-z0-9_/.\-?=]{1,200}$")
    session_name: str = Field("Server", pattern=r"^[A-Za-z0-9 _.\-]{0,64}$")
    # Unprivileged ports only: a game server has no business below 1024, and the Windows
    # firewall helper refuses to open anything there.
    port: int = Field(7777, ge=1024, le=65535)
    query_port: int = Field(27015, ge=1024, le=65535)
    max_players: int = Field(16, ge=1, le=512)
    extra_args: str = Field("", max_length=500)
    # -onethread: Ramjet's advice when several dedicated servers share one machine.
    single_thread: bool = False


class RconConfig(BaseModel):
    enabled: bool = False
    port: int = Field(27020, ge=1, le=65535)
    password: str = Field("", max_length=128)


class BehaviorConfig(BaseModel):
    auto_start: bool = False
    keep_alive: bool = True
    update_on_start: bool = False
    steam_branch: str = Field("public", pattern=r"^[A-Za-z0-9_.\-]{1,64}$")
    ram_limit_gb: int = Field(0, ge=0, le=1024)
    update_check: bool = True  # ask GitHub whether a newer manager release exists
    update_prereleases: bool = False  # also offer beta and release-candidate builds
    # How often to look for a newer manager release and a newer game server build.
    # Stored in minutes so the UI can offer minutes or hours: 10 minutes to 24 hours.
    update_check_minutes: int = Field(360, ge=10, le=1440)
    game_update_check: bool = True  # tell me when Steam has a newer game server build
    game_update_auto: bool = False  # ...and install it while the server is online with nobody on it


class ScheduleConfig(BaseModel):
    enabled: bool = False
    times: list[str] = ["04:00"]
    days: list[bool] = [True] * 7  # Monday first
    warnings: list[int] = [30, 10, 5, 1]
    uptime_enabled: bool = False
    uptime_hours: int = Field(24, ge=1, le=720)

    @field_validator("times")
    @classmethod
    def _times(cls, v: list[str]) -> list[str]:
        for t in v:
            if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t):
                raise ValueError(f"'{t}' is not a HH:MM time")
        return sorted(set(v))

    @field_validator("days")
    @classmethod
    def _days(cls, v: list[bool]) -> list[bool]:
        if len(v) != 7:
            raise ValueError("days must have 7 entries")
        return v

    @field_validator("warnings")
    @classmethod
    def _warnings(cls, v: list[int]) -> list[int]:
        return sorted({w for w in v if 0 < w <= 720}, reverse=True)


class BackupConfig(BaseModel):
    name_format: str = Field("Server_Backup_%Y-%m-%d_%H-%M-%S", pattern=r"^[A-Za-z0-9 _.%\-]{1,80}$")
    retention: int = Field(20, ge=0, le=1000)
    on_stop: bool = False
    interval_hours: int = Field(0, ge=0, le=720)


class DiscordConfig(BaseModel):
    webhook_enabled: bool = False
    webhook_url: str = ""
    bot_enabled: bool = False
    bot_token: str = Field("", max_length=200)
    channel_id: str = Field("", pattern=r"^\d{0,25}$")
    # Role IDs (comma separated) whose members may use !start, !stop, !restart and !backup, besides
    # members who can manage the Discord server.
    command_roles: str = Field("", max_length=500, pattern=r"^[\d, ]*$")
    live_status_message_id: str = Field("", pattern=r"^\d{0,25}$")

    def command_role_ids(self) -> set[int]:
        return {int(r) for r in self.command_roles.replace(" ", "").split(",") if r}

    @field_validator("webhook_url")
    @classmethod
    def _webhook(cls, v: str) -> str:
        v = v.strip()
        if v and not _WEBHOOK.match(v):
            raise ValueError("must be a https://discord.com/api/webhooks/... URL")
        return v


class NetworkConfig(BaseModel):
    # Listen on all interfaces so other devices can reach the manager (needs an admin password).
    remote_access: bool = False
    # Ask the router to forward the game ports (UPnP).
    upnp: bool = False
    # Which network adapter the router is on. Empty: the one internet traffic uses (the usual case).
    # Named when that guess is wrong, for example with a VPN that takes all traffic.
    upnp_adapter: str = Field("", max_length=100, pattern=r"^[^\x00-\x1f]*$")
    # Port Probe Service used by the optional "check from the internet". Empty turns the check off.
    probe_url: str = Field("https://be1.ferretech.org/v1/probe", pattern=r"^(https://[A-Za-z0-9.\-]{1,253}(:\d{1,5})?/[\w\-./]{0,200})?$")
    # Extra names the manager answers to, comma separated (e.g. a DNS name on your network or a
    # reverse proxy's). IP addresses, localhost and this computer's own name always work.
    allowed_hosts: str = Field("", max_length=500, pattern=r"^[A-Za-z0-9.\-, ]*$")

    def allowed_host_names(self) -> frozenset[str]:
        return frozenset(h.strip().lower().rstrip(".") for h in self.allowed_hosts.split(",") if h.strip())


class ManagerConfig(BaseModel):
    launch: LaunchConfig = LaunchConfig()
    network: NetworkConfig = NetworkConfig()
    rcon: RconConfig = RconConfig()
    behavior: BehaviorConfig = BehaviorConfig()
    schedule: ScheduleConfig = ScheduleConfig()
    backups: BackupConfig = BackupConfig()
    discord: DiscordConfig = DiscordConfig()


def _bool(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


ENV_OVERRIDES = {
    "VSM_GAME_PORT": ("launch", "port", int),
    "VSM_QUERY_PORT": ("launch", "query_port", int),
    "VSM_MAX_PLAYERS": ("launch", "max_players", int),
    "VSM_SESSION_NAME": ("launch", "session_name", str),
    "VSM_EXTRA_ARGS": ("launch", "extra_args", str),
    "VSM_STEAM_BRANCH": ("behavior", "steam_branch", str),
    "VSM_AUTO_START": ("behavior", "auto_start", _bool),
    "VSM_KEEP_ALIVE": ("behavior", "keep_alive", _bool),
    "VSM_UPDATE_ON_START": ("behavior", "update_on_start", _bool),
    "VSM_DISCORD_WEBHOOK_URL": ("discord", "webhook_url", str),
    "VSM_PROBE_URL": ("network", "probe_url", str),
    "VSM_ALLOWED_HOSTS": ("network", "allowed_hosts", str),
}


def _describe(err: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in err.errors())


class ConfigStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
        try:
            self._cfg = ManagerConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except FileNotFoundError:
            self._cfg = ManagerConfig()
        except (ValueError, ValidationError) as e:
            raise SystemExit(f"{path} is invalid, fix or delete it: {e}")
        env = {}
        for name, (section, key, cast) in ENV_OVERRIDES.items():
            if os.environ.get(name):
                env.setdefault(section, {})[key] = cast(os.environ[name])
        if env.get("discord", {}).get("webhook_url"):
            env["discord"]["webhook_enabled"] = True
        if env:
            try:
                self.update(env)
            except VsmError as e:
                raise SystemExit(f"Invalid VSM_* environment configuration: {e}")
        else:
            self._save()

    def get(self) -> ManagerConfig:
        return self._cfg

    def public(self) -> dict:
        """Config as sent to the UI: secrets are replaced by a mask when set."""
        data = self._cfg.model_dump()
        for section, key in SECRET_FIELDS:
            if data[section][key]:
                data[section][key] = MASK
        return data

    def check(self, patch: dict) -> None:
        """Raise VsmError if `patch` would not be accepted, without changing anything."""
        self._merged(patch)

    def _merged(self, patch: dict) -> ManagerConfig:
        data = self._cfg.model_dump()
        for section, values in patch.items():
            if section not in data or not isinstance(values, dict):
                raise VsmError(f"Unknown config section '{section}'.")
            for key, value in values.items():
                if (section, key) in SECRET_FIELDS and value == MASK:
                    continue  # unchanged secret echoed back by the UI
                data[section][key] = value
        try:
            return ManagerConfig.model_validate(data)
        except ValidationError as e:
            raise VsmError(_describe(e))

    def update(self, patch: dict) -> ManagerConfig:
        with self._lock:
            self._cfg = self._merged(patch)
            self._save()
            return self._cfg

    def _save(self) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(self._cfg.model_dump_json(indent=2), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)  # holds the bot token and RCON password
        except OSError:
            pass
        os.replace(tmp, self.path)
