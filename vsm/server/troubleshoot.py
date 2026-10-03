"""Looks through the game server's own log for the problems Ramjet's troubleshooting page describes.

https://ramjet.notion.site/Troubleshooting-279f9ec29f17808b82b6d634e66c7f58

The wording is matched loosely and in both the documented and the current form, because the
game's messages change between builds (the heartbeat one already has).
"""
from __future__ import annotations

import re
from pathlib import Path

MAX_LOG_BYTES = 4 * 1024 * 1024  # the end of the log is what describes the current run
EOS_SPAM = 25  # this many lines before it is worth mentioning

_STEAM_LIBRARY = re.compile(r"SteamAPI_Init\(\).*Sys_LoadModule failed|dlopen failed trying to load.*steamclient", re.IGNORECASE)
_STEAM_INIT = re.compile(r"Steam Dedicated Server API failed to initialize", re.IGNORECASE)
_HEARTBEAT_FAILED = re.compile(r"Failed to get connection string for heartbeat|Failed to heartbeat \(no connection string\)", re.IGNORECASE)
_STEAM_CONNECTED = re.compile(r"Heartbeating with IP|logged on to Steam|S_OnSteamServersConnected", re.IGNORECASE)
_LISTED_AS = re.compile(r"Heartbeating with IP ([0-9a-fA-F.:\[\]]+)")
_EOS = re.compile(r"LogOnline(Session)?:.*EOS", re.IGNORECASE)


def read_log(path: Path) -> list[str] | None:
    """The last few megabytes of the game log, or None when there is no log yet."""
    try:
        with open(path, "rb") as f:  # opened briefly and never held: the server rotates this file
            size = f.seek(0, 2)
            f.seek(max(0, size - MAX_LOG_BYTES))
            return f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return None


def scan(lines: list[str], windows: bool, running: bool) -> list[dict]:
    """Findings as {id, level, title, detail}; level is ok, info, warn or error."""
    found: list[dict] = []

    def add(ident: str, level: str, title: str, detail: str) -> None:
        found.append({"id": ident, "level": level, "title": title, "detail": detail})

    if any(_STEAM_LIBRARY.search(line) for line in lines):
        add("steam_library", "error", "The server could not load Steam's library",
            "Copy steam_api64.dll from any Steam install into the server's Vein\\Binaries\\Win64 folder, then start the server again."
            if windows else
            "Steam's steamclient.so was not where the server looks for it (~/.steam/sdk64). The manager puts it there when it starts the server: start the server from here, or link it yourself from the steamcmd linux64 folder.")

    if any(_STEAM_INIT.search(line) for line in lines):
        add("steam_init", "error", "Steam did not start inside the server",
            "The server will not appear in the server browser. Ramjet's advice for Windows is that the server may need to run with administrator rights; this is a known issue on their side.")

    failures = [i for i, line in enumerate(lines) if _HEARTBEAT_FAILED.search(line)]
    connected = [i for i, line in enumerate(lines) if _STEAM_CONNECTED.search(line)]
    listed = next((m.group(1).rstrip(".") for line in reversed(lines) if (m := _LISTED_AS.search(line))), None)
    if failures and (not connected or connected[-1] < failures[-1]):
        if running:
            add("heartbeat", "warn", "Steam cannot work out this server's address",
                "The server keeps failing to register with Steam, so players will not see it in the browser. A few of these lines right after a start are normal; if they continue, check the internet connection and that the game ports are forwarded.")
    elif listed:
        add("listed", "ok", "Registered with Steam", f"The server is announcing itself as {listed}.")
    elif connected:
        add("listed", "ok", "Connected to Steam", "The server reached Steam.")

    spam = sum(1 for line in lines if _EOS.search(line))
    if spam >= EOS_SPAM:
        add("eos_spam", "info", "The log is filling with EOS messages",
            f"{spam} harmless lines so far. To quiet them, add a [Core.Log] section to Engine.ini with LogOnlineSession=Warning and LogOnline=Warning.")

    if not any(f["level"] in ("warn", "error") for f in found):
        found.insert(0, {"id": "clean", "level": "ok", "title": "No known problems in the game log", "detail": ""})
    return found


def check(log_path: Path, windows: bool, running: bool) -> dict:
    lines = read_log(log_path)
    if lines is None:
        return {"log_found": False, "findings": []}
    return {"log_found": True, "findings": scan(lines, windows, running)}
