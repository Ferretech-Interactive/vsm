"""Version parsing and the "a newer release exists" check.

Releases are tagged `v1.2.3` (stable) or `v1.2.3-beta.1` / `-alpha.N` / `-rc.N`
(pre-release). The Python package uses the PEP 440 spelling of the same thing
(`1.2.3b1`), so both forms parse to one comparable tuple.
"""
from __future__ import annotations

import json
import re
import time
import urllib.request

from .. import __version__

REPO_URL = "https://github.com/Ferretech-Interactive/vsm"
RELEASES_API = "https://api.github.com/repos/Ferretech-Interactive/vsm/releases?per_page=30"

_VERSION = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(alpha|beta|rc|a|b)[-.]?(\d+))?$", re.IGNORECASE)
_STAGES = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "rc": 2}
_FINAL = 3

Version = tuple[int, int, int, int, int]


def parse(text: str) -> Version | None:
    """'v0.1.0-beta.2' and '0.1.0b2' both become (0, 1, 0, 1, 2); a stable release sorts after its pre-releases."""
    m = _VERSION.match(text.strip())
    if not m:
        return None
    stage = _STAGES[m.group(4).lower()] if m.group(4) else _FINAL
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), stage, int(m.group(5) or 0)


def is_prerelease(version: Version) -> bool:
    return version[3] != _FINAL


class Updates:
    def __init__(self, manager):
        self.m = manager
        self._cached: dict | None = None
        self.release: dict | None = None  # GitHub's record of the newer release, when there is one
        self._checked_at = 0.0

    def _fetch(self) -> list[dict]:
        request = urllib.request.Request(RELEASES_API, headers={"User-Agent": "VeinServerManager", "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(request, timeout=8) as r:
            return json.loads(r.read().decode())

    def check(self, force: bool = False) -> dict:
        """Never raises: with no network (or checks turned off) it just reports nothing new.
        `force` asks GitHub now instead of using the remembered answer."""
        cfg = self.m.config.get().behavior
        result = {"current": __version__, "available": False, "latest": None, "url": None, "prerelease": False}
        current = parse(__version__)
        if not cfg.update_check or current is None:
            return result
        key = cfg.update_prereleases
        if not force and self._cached and self._cached["key"] == key and time.time() - self._checked_at < cfg.update_check_minutes * 60:
            return self._cached["result"]
        try:
            releases = self._fetch()
        except Exception:
            releases = []
        # Someone already running a beta keeps hearing about betas.
        include_prereleases = cfg.update_prereleases or is_prerelease(current)
        best: tuple[Version, dict] | None = None
        for release in releases if isinstance(releases, list) else []:
            version = parse(str(release.get("tag_name", "")))
            url = str(release.get("html_url", ""))
            if version is None or release.get("draft") or not url.startswith(REPO_URL + "/"):
                continue
            if is_prerelease(version) and not include_prereleases:
                continue
            if best is None or version > best[0]:
                best = (version, release)
        self.release = best[1] if best and best[0] > current else None
        if best and best[0] > current:
            result.update(available=True, latest=best[1]["tag_name"], url=best[1]["html_url"], prerelease=is_prerelease(best[0]))
        self._cached, self._checked_at = {"key": key, "result": result}, time.time()
        return result
