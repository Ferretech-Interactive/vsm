"""What a Linux server is, and which kind of install it can take. Read-only: nothing is changed.

`SCRIPT` prints `key=value` lines; `parse` reads them defensively (they come from a machine we do
not control) and `assess` turns them into the choice shown in the Deploy window.
"""
from __future__ import annotations

import re

INSTALL_DIR = "/opt/vein-server-manager"
WEB_PORT = 8810
MIN_PYTHON = (3, 11)
MIN_FREE_GB = 15

# Fixed text, sent to `sh -s` on standard input. No value from the person appears in it.
SCRIPT = rf"""
echo "arch=$(uname -m)"
if [ -r /etc/os-release ]; then . /etc/os-release; fi
echo "os_id=${{ID:-}}"
echo "os_version=${{VERSION_ID:-}}"
echo "os_name=${{PRETTY_NAME:-}}"
echo "python=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)"
if [ -d /run/systemd/system ]; then echo systemd=1; else echo systemd=0; fi
if command -v docker >/dev/null 2>&1; then echo docker=1; else echo docker=0; fi
if docker compose version >/dev/null 2>&1; then echo compose=1; else echo compose=0; fi
for pm in apt-get dnf yum zypper pacman; do
    if command -v "$pm" >/dev/null 2>&1; then echo "pkg=$pm"; break; fi
done
dir=/opt; [ -d {INSTALL_DIR} ] && dir={INSTALL_DIR}
echo "free_gb=$(df -Pk "$dir" 2>/dev/null | awk 'NR==2 {{print int($4 / 1048576)}}')"
if [ -e {INSTALL_DIR}/vsm.env ]; then echo existing=native; elif [ -e {INSTALL_DIR}/.env ]; then echo existing=docker; fi
if (ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) | grep -q ':{WEB_PORT} '; then echo port_busy=1; else echo port_busy=0; fi
"""

# Run as root afterwards: sshd's effective setting cannot be read without it.
FORWARDING = ["sh", "-c", "PATH=$PATH:/usr/sbin:/sbin; sshd -T 2>/dev/null | grep -i '^allowtcpforwarding' || true"]

# Distributions Docker's own install script (used by install.sh) covers.
_DOCKER_SCRIPT = {"ubuntu", "debian", "raspbian", "fedora", "centos", "rhel", "rocky"}
_NATIVE_PACKAGES = {"apt-get", "dnf", "yum", "pacman"}
_LINE = re.compile(r"^([a-z_]{1,20})=(.{0,200})$")


def parse(text: str) -> dict:
    facts: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE.match(line.strip())
        if m:
            facts[m.group(1)] = re.sub(r"[^\x20-\x7e]", "", m.group(2)).strip()
    python = re.match(r"^(\d+)\.(\d+)$", facts.get("python", ""))
    return {
        "arch": facts.get("arch", ""),
        "os_id": facts.get("os_id", "").lower(),
        "os_name": facts.get("os_name") or facts.get("os_id") or "Linux",
        "python": (int(python.group(1)), int(python.group(2))) if python else None,
        "systemd": facts.get("systemd") == "1",
        "docker": facts.get("docker") == "1",
        "compose": facts.get("compose") == "1",
        "pkg": facts.get("pkg", ""),
        "free_gb": int(facts["free_gb"]) if facts.get("free_gb", "").isdigit() else None,
        "existing": facts.get("existing") if facts.get("existing") in ("native", "docker") else None,
        "port_busy": facts.get("port_busy") == "1",
    }


def forwarding_allowed(text: str) -> bool:
    """`sshd -T` output for AllowTcpForwarding. Anything but a clear "no" counts as allowed:
    the tunnel itself is the real test, this only gives an early, clearer message."""
    m = re.search(r"allowtcpforwarding\s+(\S+)", text, re.IGNORECASE)
    return not m or m.group(1).lower() in ("yes", "local", "all")


def assess(facts: dict, forwarding: bool = True) -> dict:
    """The choices for the Deploy window: each kind is usable or comes with the reason it is not."""
    blocking: list[str] = []
    notes: list[str] = []
    if facts["arch"] != "x86_64":
        blocking.append(f"This server's processor is {facts['arch'] or 'unknown'}. The Vein server only runs on 64-bit x86 (x86_64).")
    if not forwarding:
        blocking.append("This server's SSH service has port forwarding turned off (AllowTcpForwarding no), which the app needs to reach the manager. Turn it on in sshd_config and try again.")

    python = facts["python"]
    if not facts["systemd"]:
        native = "This server does not use systemd."
    elif python is None:
        native = None if facts["pkg"] in _NATIVE_PACKAGES else "Python 3 is not installed and this distribution's package manager is not one the installer knows."
    elif python < MIN_PYTHON:
        native = f"This server has Python {python[0]}.{python[1]}; the manager needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer."
    elif facts["pkg"] not in _NATIVE_PACKAGES:
        native = "This distribution's package manager is not one the installer knows."
    else:
        native = None

    if facts["docker"] and facts["compose"]:
        docker = None
    elif facts["docker"]:
        docker = None if facts["pkg"] in ("apt-get", "dnf", "yum") else "Docker is installed but Docker Compose v2 is not. Install it on the server first."
    else:
        docker = None if facts["os_id"] in _DOCKER_SCRIPT else "Docker is not installed, and it cannot be installed automatically on this distribution. Install Docker and Compose on the server first."
        if docker is None and not facts["existing"]:
            notes.append("Docker is not installed on this server; choosing Docker installs it.")

    existing = facts["existing"]
    if existing:
        notes.append(f"Vein Server Manager is already installed here ({'as a service' if existing == 'native' else 'in Docker'}). It will be updated to this app's version and keep its data and password. From then on it is reachable through SSH only, not directly on the network.")
        other = "docker" if existing == "native" else "native"
        reason = "It is already installed the other way on this server."
        native, docker = (reason if other == "native" else native), (reason if other == "docker" else docker)
    elif facts["port_busy"]:
        notes.append(f"Something on this server is already using port {WEB_PORT}; the manager may not be able to start.")
    if facts["free_gb"] is not None and facts["free_gb"] < MIN_FREE_GB:
        notes.append(f"Only {facts['free_gb']} GB is free on this server; the game server needs about {MIN_FREE_GB} GB.")

    if native is not None and docker is not None:
        blocking.append("Neither kind of install can be made on this server as it is.")
    recommended = existing or ("native" if native is None else "docker" if docker is None else None)
    return {
        "os": facts["os_name"],
        "existing": existing,
        "native": {"ok": native is None, "reason": native},
        "docker": {"ok": docker is None, "reason": docker},
        "recommended": None if blocking else recommended,
        "blocking": blocking,
        "notes": notes,
    }
