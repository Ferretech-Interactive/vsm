"""The diagnostics zip behind "Report a problem": logs and settings that help with a bug report.

The zip is meant to be attached to a public issue, so what goes in is chosen, not swept up:
never `auth.json`, sessions, keys, saves or backups. Every text file passes through `Scrubber`,
which always removes secrets and, when asked (the default), also replaces public IP addresses,
SteamIDs, player names and the Windows/Linux user name with placeholders.
"""
from __future__ import annotations

import io
import ipaddress
import json
import os
import platform
import re
import secrets
import sys
import threading
import time
import zipfile
from pathlib import Path

from .. import __version__

LOG_TAIL_BYTES = 1_000_000  # per log file, from the end
DESKTOP_LOG_MAX = 1_000_000
DESKTOP_LOG_TTL = 1800  # a desktop log handed over by the app is attached to bundles for 30 minutes

_REMOVED = "<removed>"
_SECRET_PATTERNS = [
    # Discord webhook URLs and bot tokens, bearer tokens.
    (re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\S+", re.I), "<discord webhook>"),
    (re.compile(r"\b[MNO][A-Za-z\d_-]{23,27}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27,}\b"), "<discord token>"),
    (re.compile(r"(\bBearer\s+)[^\s\"']+", re.I), r"\1" + _REMOVED),
    # key=value / key: value / "key": "value" where the key names a secret. A ?Password= in an
    # Unreal URL counts too. Values stop at whitespace, quotes, commas and URL separators.
    (re.compile(r"""(?P<k>["']?[\w.-]*(?:password|passwd|token|secret|api[_-]?key)[\w.-]*["']?\s*[=:]\s*["']?)(?P<v>[^\s"',&?]+)""", re.I),
     r"\g<k>" + _REMOVED),
]
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
_STEAM_ID = re.compile(r"(?<!\d)7656119\d{10}(?!\d)")
_USER_DIR = re.compile(r"(?i)([\\/](?:Users|home)[\\/])([^\\/\s\"']+)")
_KEEP_USERS = {"public", "default", "all users", "<user>"}


class Scrubber:
    """Removes secrets from text; with `anonymize`, also who and where (consistently, so the
    same player is the same placeholder in every file of one zip)."""

    def __init__(self, secrets: list[str], anonymize: bool, names: list[str] | None = None):
        # Literal secret values first, longest first, so a password that does not look like one
        # (no "password=" in front of it) is still caught wherever it was echoed.
        self.secrets = sorted({s for s in secrets if s and len(s) >= 4 and s != "********"}, key=len, reverse=True)
        self.anonymize = anonymize
        self.names = sorted({n for n in (names or []) if n and len(n) >= 3}, key=len, reverse=True)
        self._seen: dict[str, str] = {}

    def _placeholder(self, kind: str, value: str) -> str:
        key = f"{kind}:{value}"
        if key not in self._seen:
            count = sum(1 for k in self._seen if k.startswith(kind + ":")) + 1
            self._seen[key] = f"<{kind}-{count}>"
        return self._seen[key]

    def _ip(self, match: re.Match) -> str:
        text = match.group(1)
        try:
            address = ipaddress.IPv4Address(text)
        except ValueError:
            return text  # a version number or the like
        if not address.is_global:
            return text  # private and special addresses help with network problems and say nothing about where it is
        return self._placeholder("ip", text)

    def text(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, _REMOVED)
        for pattern, replacement in _SECRET_PATTERNS:
            text = pattern.sub(replacement, text)
        if self.anonymize:
            text = _STEAM_ID.sub(lambda m: self._placeholder("steamid", m.group(0)), text)
            text = _IPV4.sub(self._ip, text)
            for name in self.names:
                text = re.sub(rf"(?<![\w]){re.escape(name)}(?![\w])", lambda m, n=name: self._placeholder("player", n), text)
            text = _USER_DIR.sub(lambda m: m.group(1) + (m.group(2) if m.group(2).lower() in _KEEP_USERS else "<user>"), text)
        return text


def tail(path: Path, limit: int = LOG_TAIL_BYTES) -> str | None:
    """The end of a text file, starting at a whole line; None when there is no such file."""
    try:
        with open(path, "rb") as f:
            size = f.seek(0, os.SEEK_END)
            f.seek(max(0, size - limit))
            data = f.read()
    except OSError:
        return None
    if size > limit:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace")


def _with_rotated(path: Path, limit: int = LOG_TAIL_BYTES) -> str | None:
    """A rotating log's newest file, preceded by the end of the previous one when it is short."""
    current = tail(path, limit)
    if current is None:
        return None
    if len(current) < limit // 2:
        previous = tail(path.with_name(path.name + ".1"), limit - len(current))
        if previous:
            current = previous + current
    return current


def _events(m) -> str:
    lines = []
    for e in m.hub.snapshot("event"):
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(e.get("time", 0)))
        lines.append(f"{when} [{e.get('category', '')}] {e.get('message', '')}")
    return "\n".join(lines) + "\n"


def install_kind(m) -> str:
    """Worded as the bug report form's options, so the page can fill the form in."""
    if m.service_mode:
        return "Windows, installed (Windows service)"
    if os.path.exists("/.dockerenv"):
        return "Linux, Docker"
    if sys.platform.startswith("linux"):
        return "Linux, native (install.sh / systemd)"
    if getattr(sys, "frozen", False):
        return "Windows, portable (zip)"
    return "From source"


def summary(m) -> dict:
    import psutil

    status = m.server.status()
    config = m.config.get()
    disk = psutil.disk_usage(str(m.paths.data))
    return {
        "version": __version__,
        "install": install_kind(m),
        "os": platform.platform(),
        "python": platform.python_version(),
        "created": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "server": {
            "state": status["state"],
            "task": status["task"],
            "installed": status["installed"],
            "build_id": status["build_id"],
            "branch": config.behavior.steam_branch,
            "uptime_seconds": status["uptime"],
            "crash_count": status["crash_count"],
            "players_online": len(status["players"]),
        },
        "machine": {
            "cpus": psutil.cpu_count(),
            "ram_gb": round(psutil.virtual_memory().total / 1024**3, 1),
            "disk_free_gb": round(disk.free / 1024**3, 1),
        },
        "features": {
            "upnp": config.network.upnp,
            "remote_access": config.network.remote_access,
            "discord_webhook": config.discord.webhook_enabled,
            "discord_bot": config.discord.bot_enabled,
            "ue4ss": m.ue4ss.supported and m.paths.ue4ss_proxy.exists(),
            "schedule": config.schedule.enabled,
            "auto_start": config.behavior.auto_start,
        },
    }


def _secrets(m) -> list[str]:
    config = m.config.get().model_dump()
    from .config import SECRET_FIELDS

    values = [str(config.get(section, {}).get(key) or "") for section, key in SECRET_FIELDS]
    try:
        values.append(m.game.read().get("password", ""))
    except Exception:
        pass
    values += [os.environ.get(k, "") for k in ("VSM_ADMIN_PASSWORD", "VSM_API_TOKEN", "VSM_SERVER_PASSWORD", "VSM_RCON_PASSWORD")]
    return values


def _names(m) -> list[str]:
    names = [n for n in m.server.sessions.values()]
    try:
        names += [p.get("name", "") for p in m.history.list()]
    except Exception:
        pass
    return names


def contents(m, desktop_log: bool) -> list[str]:
    """What a bundle holds, for the page to show before anything is downloaded."""
    return [name for name, _ in _files(m, desktop_log, health=True)]


def _files(m, desktop_log: bool, health):
    """(name in the zip, function returning its text) for every file that exists.
    `health` runs the health check (the API's, passed in to keep this module free of routes)."""
    p = m.paths

    def item(name, produce):
        def read():
            try:
                return produce()
            except Exception as e:  # one unreadable file must not lose the rest
                return f"(could not be read: {e})\n"
        return (name, read)

    def present(path: Path) -> bool:
        return path.is_file()

    out = [
        item("summary.json", lambda: json.dumps(summary(m), indent=2)),
        item("config.json", lambda: json.dumps(m.config.public(), indent=2)),
        item("events.txt", lambda: _events(m)),
    ]
    if health:
        out.append(item("health-check.json", lambda: json.dumps(health(), indent=2)))
    for name, path in (("logs/manager.log", p.logs / "manager.log"), ("logs/audit.log", p.logs / "audit.log"),
                       ("logs/steamcmd.log", p.logs / "steamcmd.log")):
        if present(path):
            out.append(item(name, lambda path=path: _with_rotated(path)))
    if present(p.game_log):
        out.append(item("game/Vein.log", lambda: tail(p.game_log)))
    for path in (p.game_ini, p.engine_ini):
        if present(path):
            out.append(item(f"game/{path.name}", lambda path=path: path.read_text(encoding="utf-8", errors="replace")))
    out.append(item("game/mods.json", lambda: json.dumps({"paks": m.mods.list(), "logic_mods": m.logic_mods.list()}, indent=2)))
    if m.ue4ss.supported:
        for name, path in (("ue4ss/UE4SS.log", p.ue4ss_dir / "UE4SS.log"), ("ue4ss/mods.txt", p.ue4ss_dir / "Mods" / "mods.txt"),
                           ("ue4ss/UE4SS-settings.ini", p.ue4ss_dir / "UE4SS-settings.ini")):
            if present(path):
                out.append(item(name, lambda path=path: tail(path)))
    if desktop_log:
        out.append(item("app/desktop.log", lambda: m.desktop_log[1]))
    elif present(p.logs / "desktop.log"):  # portable: the app and the manager share one data folder
        out.append(item("app/desktop.log", lambda: tail(p.logs / "desktop.log")))
    return out


def bundle(m, anonymize: bool, health=None, progress=None) -> bytes:
    """The zip itself, built in memory (every file is capped, so it stays small).
    `progress(step, done, total)` hears what is being added, for the page's progress bar."""
    scrub = Scrubber(_secrets(m), anonymize, _names(m) if anonymize else None)
    has_desktop_log = bool(m.desktop_log and time.time() - m.desktop_log[0] < DESKTOP_LOG_TTL)
    files = _files(m, has_desktop_log, health)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for done, (name, read) in enumerate(files):
            if progress:
                progress("Running the health check" if name == "health-check.json" else f"Adding {name}", done, len(files))
            z.writestr(name, scrub.text(read() or ""))
        z.writestr("README.txt", (
            "Vein Server Manager diagnostics\n\n"
            "Secrets (passwords, tokens, Discord webhook URLs) were removed from every file.\n"
            + ("Public IP addresses, SteamIDs, player names and the user name were replaced with placeholders.\n"
               if anonymize else "Public IP addresses, SteamIDs and player names were left in.\n")
            + "Not included: the password file, sessions, keys, saves and backups.\n"
            "Check the files before posting them anywhere public.\n"
        ))
    if progress:
        progress("Done", len(files), len(files))
    return buffer.getvalue()


class Job:
    """One diagnostics zip built in the background, so the page can show how far along it is.
    One at a time (a second click joins the first); the zip is kept until fetched, at most ten minutes."""

    KEEP = 600

    def __init__(self, m):
        self.m = m
        self.lock = threading.Lock()
        self.id = ""
        self.state = "idle"  # idle | working | done | failed
        self.step, self.done, self.total = "", 0, 1
        self.error = ""
        self.data: bytes | None = None
        self.name = ""
        self.anonymize = True
        self.finished_at = 0.0

    def start(self, anonymize: bool, health=None) -> str:
        with self.lock:
            if self.state == "working":
                return self.id
            self.id = secrets.token_hex(8)
            self.state, self.step, self.done, self.total, self.error, self.data = "working", "Starting", 0, 1, "", None
            self.anonymize = anonymize
            job_id = self.id
        threading.Thread(target=self._run, args=(job_id, anonymize, health), name="support-bundle", daemon=True).start()
        return job_id

    def _run(self, job_id: str, anonymize: bool, health) -> None:
        def progress(step, done, total):
            with self.lock:
                if self.id == job_id:
                    self.step, self.done, self.total = step, done, total

        try:
            data = bundle(self.m, anonymize, health, progress)
            with self.lock:
                if self.id == job_id:
                    self.data, self.name, self.state = data, file_name(), "done"
        except Exception as e:
            with self.lock:
                if self.id == job_id:
                    self.state, self.error = "failed", str(e) or e.__class__.__name__
        self.finished_at = time.time()

    def status(self, job_id: str) -> dict | None:
        with self.lock:
            if job_id != self.id or self.state == "idle":
                return None
            if self.state == "done" and time.time() - self.finished_at > self.KEEP:
                self.state, self.data = "idle", None
                return None
            return {"state": self.state, "step": self.step, "done": self.done, "total": self.total, "error": self.error}

    def take(self, job_id: str) -> tuple[str, bytes, bool] | None:
        """The finished zip, once: it is dropped from memory when handed over."""
        with self.lock:
            if job_id != self.id or self.state != "done" or self.data is None:
                return None
            data, self.data, self.state = self.data, None, "idle"
            return self.name, data, self.anonymize


def file_name() -> str:
    return f"vsm-diagnostics-{__version__}-{time.strftime('%Y%m%d-%H%M%S')}.zip"
