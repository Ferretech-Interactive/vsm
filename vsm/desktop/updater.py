"""One-click update for the installed Windows program.

Runs in the desktop window's process, as the person at the PC, never in the service: the
service account has no right to install anything, and that is deliberate. The flow is

    ask GitHub for releases -> pick the newest stable one -> the person says yes ->
    download Setup -> check its signature -> run it elevated (one UAC prompt) in update mode

Only signed builds are installed this way. A file that is not validly signed by the expected
publisher is deleted, whatever it claims to be; pre-releases are not signed, so they are only
pointed to. Nothing here takes input from a web page: a page can at most ask for this to start.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import urllib.request
from pathlib import Path

from ..core.updates import RELEASES_API, REPO_URL, is_prerelease, parse

log = logging.getLogger("vsm.desktop")

PUBLISHER = "FerreTech Interactive LLC"  # the code-signing certificate's common name
ISSUER = "Certum"  # start of the common name of the authority that issues it
DOWNLOAD_PREFIX = REPO_URL + "/releases/download/"
# GitHub stores uploaded names with dots for spaces ("VSM.0.2.0.Setup.exe"); both spellings are accepted.
SETUP_NAME = re.compile(r"^VSM[ .][0-9A-Za-z.\-]{1,32}[ .]Setup\.exe$")
MAX_SETUP_BYTES = 400 * 1024 * 1024
SETUP_ARGUMENTS = "/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /RELAUNCH=1"


class UpdateError(Exception):
    pass


def fetch_releases() -> list:
    request = urllib.request.Request(RELEASES_API, headers={"User-Agent": "VeinServerManager", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=10) as r:
        data = json.loads(r.read(4 * 1024 * 1024).decode())
    return data if isinstance(data, list) else []


def _setup_asset(release: dict) -> dict | None:
    for asset in release.get("assets") or []:
        name, url = str(asset.get("name", "")), str(asset.get("browser_download_url", ""))
        if SETUP_NAME.match(name) and url.startswith(DOWNLOAD_PREFIX):
            return {"name": name, "url": url, "size": int(asset.get("size") or 0)}
    return None


def pick_release(releases: list, current: str) -> tuple[dict | None, dict | None]:
    """(newest stable release newer than `current` that carries a Setup.exe, newest newer pre-release).

    Each is {"tag", "page", "name", "url", "size"}; the pre-release has no download fields.
    """
    now = parse(current)
    if now is None:
        return None, None
    stable: tuple | None = None
    beta: tuple | None = None
    for release in releases:
        if not isinstance(release, dict) or release.get("draft"):
            continue
        version = parse(str(release.get("tag_name", "")))
        page = str(release.get("html_url", ""))
        if version is None or version <= now or not page.startswith(REPO_URL + "/"):
            continue
        entry = {"tag": str(release["tag_name"]), "page": page}
        if is_prerelease(version) or release.get("prerelease"):
            if beta is None or version > beta[0]:
                beta = (version, entry)
        elif (asset := _setup_asset(release)) and (stable is None or version > stable[0]):
            stable = (version, {**entry, **asset})
    if stable and beta and beta[0] < stable[0]:
        beta = None  # an older beta is of no interest once a newer stable release exists
    return (stable[1] if stable else None), (beta[1] if beta else None)


def download(release: dict, folder: Path) -> Path:
    """Fetch the release's Setup.exe into `folder`. Raises UpdateError with a readable reason."""
    if not release["url"].startswith(DOWNLOAD_PREFIX) or not SETUP_NAME.match(release["name"]):
        raise UpdateError("the release does not point at a Setup file from this project")
    if release["size"] > MAX_SETUP_BYTES:
        raise UpdateError("the download is unexpectedly large")
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("*.exe"):  # earlier downloads are of no further use
        old.unlink(missing_ok=True)
    target = folder / release["name"]
    request = urllib.request.Request(release["url"], headers={"User-Agent": "VeinServerManager"})
    try:
        with urllib.request.urlopen(request, timeout=30) as r, open(target, "wb") as out:
            written = 0
            while chunk := r.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_SETUP_BYTES:
                    raise UpdateError("the download is unexpectedly large")
                out.write(chunk)
    except OSError as e:
        target.unlink(missing_ok=True)
        raise UpdateError(f"the download failed ({e})")
    except UpdateError:
        target.unlink(missing_ok=True)
        raise
    if release["size"] and written != release["size"]:
        target.unlink(missing_ok=True)
        raise UpdateError("the download was incomplete")
    return target


def common_name(subject: str) -> str:
    """The CN from a certificate subject such as 'CN=Some Co, O=Some Co, C=US' (quoted values allowed)."""
    m = re.search(r'(?:^|,\s*)CN=("([^"]*)"|[^,]*)', subject)
    if not m:
        return ""
    return (m.group(2) if m.group(2) is not None else m.group(1)).strip()


def is_trusted(status: str, subject: str, issuer: str) -> bool:
    """A file may be installed only when Windows says its signature is valid, it names our publisher, and
    the certificate is from the authority that issues ours (another CA's certificate in our name is refused)."""
    return status == "Valid" and common_name(subject) == PUBLISHER and common_name(issuer).startswith(ISSUER)


def signature(path: Path) -> tuple[str, str, str]:
    """(status, signer subject, its issuer) as Windows sees the file: 'Valid' means an intact file and a trusted chain."""
    system = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(system, 260)
    script = (
        "$s = Get-AuthenticodeSignature -LiteralPath $env:VSM_VERIFY_FILE;"
        "$c = $s.SignerCertificate;"
        "[Console]::Out.Write($s.Status.ToString() + \"`n\" + $(if ($c) { $c.Subject }) + \"`n\" + $(if ($c) { $c.Issuer }))"
    )
    modules = os.path.join(system.value, r"WindowsPowerShell\v1.0\Modules")
    try:
        result = subprocess.run(
            [os.path.join(system.value, r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=120, creationflags=subprocess.CREATE_NO_WINDOW,
            # The file is passed out of band (no quoting to get wrong); modules only from Windows' own folder.
            env={**os.environ, "VSM_VERIFY_FILE": str(path), "PSModulePath": modules},
        )
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("could not check the signature: %s", e)
        return "Unknown", "", ""
    status, subject, issuer = (result.stdout.split("\n") + ["", "", ""])[:3]
    return status.strip(), subject.strip(), issuer.strip()


def _hold_open(path: Path):
    """Open the file so nothing can replace or rewrite it between the check and the launch."""
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 0x1, 3
    create = ctypes.windll.kernel32.CreateFileW
    create.restype = ctypes.c_void_p
    handle = create(str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, 0, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise UpdateError("the downloaded file could not be opened")
    return handle


def verify_and_run(path: Path) -> None:
    """Check the signature and start Setup elevated, in update mode. Raises UpdateError if it must not run."""
    handle = _hold_open(path)
    try:
        status, subject, issuer = signature(path)
        if not is_trusted(status, subject, issuer):
            log.warning("refusing to install %s: signature %s, signer %r, issuer %r", path.name, status, subject, issuer)
            raise UpdateError(f"the download is not signed by {PUBLISHER} (Windows reports: {status or 'no signature'})")
        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.restype = ctypes.c_void_p
        result = shell_execute(None, "runas", str(path), SETUP_ARGUMENTS, str(path.parent), 1)
        if (result or 0) <= 32:  # includes "the person said no to the Windows prompt"
            raise UpdateError("Setup was not started (the Windows permission prompt was declined or failed)")
    finally:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
