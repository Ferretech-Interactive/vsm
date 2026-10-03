"""Find a Steam account's SteamID64 and name from what people actually know: a profile link or name.

Uses the public profile pages of steamcommunity.com (no API key). Names are remembered in
`steam_users.json`; names for a list of IDs are filled in on a background thread, so callers
get what is known at once, plus `loading` while the rest is fetched.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import VsmError, __version__

log = logging.getLogger("vsm.steam_users")

ID64_BASE = 76561197960265728  # SteamID64 of account number 0 (individual, public universe)
MAX_AGE = 7 * 24 * 60 * 60  # names change: look again after a week
RETRY_AFTER = 60 * 60  # after a failed lookup of an ID
MAX_REPLY = 512 * 1024
MAX_BATCH = 200
PAUSE = 0.4  # between background lookups, to stay polite to Steam

_ID64 = re.compile(r"^7656119\d{10}$")
_VANITY = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
_URL = re.compile(r"^(?:https?://)?(?:www\.)?steamcommunity\.com/(profiles|id)/([^/?#\s]+)", re.IGNORECASE)
_STEAM2 = re.compile(r"^STEAM_[0-5]:([01]):(\d{1,10})$", re.IGNORECASE)
_STEAM3 = re.compile(r"^\[?U:1:(\d{1,10})\]?$", re.IGNORECASE)
_AVATAR_HOST = re.compile(r"^avatars\.([a-z0-9-]+\.)?steamstatic\.com$")  # must stay within the img-src rule in app.py


def parse_query(text: str) -> tuple[str, str]:
    """Work out what was typed. Returns ("profiles", steamid64) or ("id", custom URL name)."""
    text = text.strip()
    if _ID64.match(text):
        return "profiles", text
    if m := _STEAM2.match(text):
        return "profiles", str(ID64_BASE + int(m.group(2)) * 2 + int(m.group(1)))
    if m := _STEAM3.match(text):
        return "profiles", str(ID64_BASE + int(m.group(1)))
    if m := _URL.match(text):
        kind, value = m.group(1).lower(), urllib.parse.unquote(m.group(2))
        if kind == "profiles" and _ID64.match(value):
            return "profiles", value
        if kind == "id" and _VANITY.match(value):
            return "id", value
        raise VsmError("That does not look like a Steam profile link.")
    if text.isdigit():
        raise VsmError("A SteamID64 has 17 digits and starts with 7656119.")
    if _VANITY.match(text):
        return "id", text
    raise VsmError("Enter a Steam profile link, the custom name from a profile link, or a 17-digit SteamID64.")


def _tag(xml: str, name: str) -> str:
    m = re.search(rf"<{name}>\s*(?:<!\[CDATA\[(.*?)\]\]>|([^<]*))\s*</{name}>", xml, re.DOTALL)
    return (m.group(1) if m.group(1) is not None else m.group(2)).strip() if m else ""


def parse_profile(xml: str) -> dict | None:
    """Read a profile's `?xml=1` page. None when Steam says there is no such profile."""
    head = xml[:xml.find("<groups>")] if "<groups>" in xml else xml  # group names must not be mistaken for the player
    steam_id = _tag(head, "steamID64")
    if not _ID64.match(steam_id):
        return None
    avatar = _tag(head, "avatarMedium")
    parts = urllib.parse.urlsplit(avatar)
    if parts.scheme != "https" or not _AVATAR_HOST.match(parts.hostname or "") or len(avatar) > 300:
        avatar = ""
    name = re.sub(r"[\x00-\x1f\x7f]", "", _tag(head, "steamID"))[:64]
    return {"id": steam_id, "name": name, "avatar": avatar}


def fetch(kind: str, value: str) -> dict | None:
    url = f"https://steamcommunity.com/{kind}/{urllib.parse.quote(value, safe='')}/?xml=1"
    request = urllib.request.Request(url, headers={"User-Agent": f"VeinServerManager/{__version__}"})
    with urllib.request.urlopen(request, timeout=8) as r:
        return parse_profile(r.read(MAX_REPLY).decode("utf-8", errors="replace"))


class SteamUsers:
    def __init__(self, manager):
        self.m = manager
        self.file = manager.paths.data / "steam_users.json"
        self._lock = threading.Lock()
        self._queue: list[str] = []
        self._working = False
        self._failed: dict[str, float] = {}
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            self._users = {k: v for k, v in data.items() if _ID64.match(k) and isinstance(v, dict)}
        except (OSError, ValueError, AttributeError):
            self._users = {}

    def _remember(self, user: dict) -> None:
        with self._lock:
            self._users[user["id"]] = {"name": user["name"], "avatar": user["avatar"], "checked": time.time()}
            temp = self.file.with_suffix(".tmp")
            temp.write_text(json.dumps(self._users), encoding="utf-8")
            temp.replace(self.file)

    def lookup(self, text: str) -> dict:
        """Resolve what a person typed to one account. Blocks for one request to Steam."""
        kind, value = parse_query(text)
        try:
            user = fetch(kind, value)
        except (OSError, urllib.error.URLError) as e:
            log.info("Steam lookup failed: %s", e)
            if kind == "profiles":  # the ID itself is already known; only the name is missing
                return {"id": value, **self._known(value)}
            raise VsmError("Could not reach Steam to look that up. Check the internet connection, or enter the 17-digit SteamID64.", 502)
        if user is None:
            raise VsmError("Steam has no profile with that link or name." if kind == "id" else "Steam has no account with that SteamID.", 404)
        self._remember(user)
        return user

    def _known(self, steam_id: str) -> dict:
        stored = self._users.get(steam_id)
        if stored and stored.get("name"):
            return {"name": stored["name"], "avatar": stored.get("avatar", "")}
        seen = self.m.history.data.get(steam_id, {})  # the in-game name, if they have joined this server
        return {"name": str(seen.get("name", "")), "avatar": ""}

    def names(self, ids: list[str]) -> dict:
        """What is known about these IDs right now; the rest is looked up in the background."""
        ids = [i for i in dict.fromkeys(ids) if _ID64.match(i)][:MAX_BATCH]
        now = time.time()
        with self._lock:
            for steam_id in ids:
                fresh = now - self._users.get(steam_id, {}).get("checked", 0) < MAX_AGE
                failed = now - self._failed.get(steam_id, 0) < RETRY_AFTER
                if not fresh and not failed and steam_id not in self._queue:
                    self._queue.append(steam_id)
            if self._queue and not self._working:
                self._working = True
                threading.Thread(target=self._work, name="steam-users", daemon=True).start()
            pending = set(self._queue)
        return {"users": {i: self._known(i) for i in ids}, "loading": any(i in pending for i in ids)}

    def _work(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._working = False
                    return
                steam_id = self._queue[0]
            try:
                user = fetch("profiles", steam_id)
                if user and user["id"] == steam_id:
                    self._remember(user)
                else:
                    self._failed[steam_id] = time.time()
            except Exception as e:
                log.info("Steam name lookup for %s failed: %s", steam_id, e)
                self._failed[steam_id] = time.time()
            with self._lock:
                self._queue.remove(steam_id)
            time.sleep(PAUSE)
