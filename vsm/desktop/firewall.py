"""Keeps the Windows Firewall rules in step with the manager's settings.

`sync()` is run as SYSTEM by a scheduled task, on behalf of a service account that
must not be trusted with SYSTEM's rights. So this module is deliberately isolated:

  * it takes no input except two files it reads defensively,
  * it imports nothing from the rest of the manager and writes no files,
  * the only thing it can do is replace three fixed, narrowly scoped rules.

The service account controls `config.json`; the worst it can achieve through this
helper is opening two unprivileged UDP ports for the game server executable (and for
the manager, which listens on them only during a port-forwarding check), which is what
the feature is for.
"""
from __future__ import annotations

import ctypes
import json
import ntpath
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

GAME_RULE = "Vein Server Manager (game)"
WEB_RULE = "Vein Server Manager (web UI)"
# Lets the manager itself hear the test packets of a port-forwarding check on the game ports.
CHECK_RULE = "Vein Server Manager (port check)"
RULES = (GAME_RULE, WEB_RULE, CHECK_RULE)
LEGACY_RULES = ("Vein Server (game)",)  # created by installers before the service existed

# Where the game server lives under the data folder. Must match vsm/core/paths.py (a test checks it).
SERVER_EXE = ("server", "Vein", "Binaries", "Win64", "VeinServer-Win64-Test.exe")
MANAGER_EXE = "VeinServerManager.exe"

MIN_PORT = 1024  # never open a privileged port on the service account's say-so
MAX_FILE_BYTES = 1024 * 1024
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


class FirewallError(Exception):
    pass


@dataclass(frozen=True)
class Rule:
    name: str
    protocol: str  # "UDP" or "TCP"
    ports: tuple[int, ...]
    program: str
    profiles: str = "any"  # netsh profile list


def _read_json(path: Path) -> dict:
    """Read a small JSON object, refusing links and anything that is not a plain file."""
    try:
        info = os.lstat(path)
    except OSError as e:
        raise FirewallError(f"cannot read {path.name}: {e}")
    if not stat.S_ISREG(info.st_mode) or getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise FirewallError(f"{path.name} is not a regular file")
    if info.st_size > MAX_FILE_BYTES:
        raise FirewallError(f"{path.name} is too large")
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_FILE_BYTES + 1)
        data = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError) as e:
        raise FirewallError(f"cannot parse {path.name}: {e}")
    if len(raw) > MAX_FILE_BYTES or not isinstance(data, dict):
        raise FirewallError(f"{path.name} is not a settings file")
    return data


def _port(value, what: str) -> int:
    # `type(...) is int` on purpose: True/False are ints in Python, and floats or strings are not ports.
    if type(value) is not int or not MIN_PORT <= value <= 65535:
        raise FirewallError(f"{what} must be a whole number from {MIN_PORT} to 65535")
    return value


def read_ports(config_file: Path) -> tuple[int, int]:
    """The game and query ports from the manager's config.json."""
    launch = _read_json(config_file).get("launch")
    if not isinstance(launch, dict):
        raise FirewallError("config.json has no launch settings")
    return _port(launch.get("port"), "game port"), _port(launch.get("query_port"), "query port")


def read_remote_access(config_file: Path) -> bool:
    network = _read_json(config_file).get("network")
    return isinstance(network, dict) and network.get("remote_access") is True


def read_install(app_dir: Path) -> tuple[Path, int]:
    """Data folder and web port from desktop.json, which only administrators can write."""
    settings = _read_json(app_dir / "desktop.json")
    data_dir = settings.get("data_dir")
    if not isinstance(data_dir, str) or not Path(data_dir).is_absolute():
        raise FirewallError("desktop.json has no absolute data_dir")
    port = settings.get("port", 8810)
    if type(port) is not int or not 1 <= port <= 65535:
        raise FirewallError("desktop.json has an invalid port")
    return Path(data_dir), port


def desired_rules(app_dir: Path) -> list[Rule]:
    data_dir, web_port = read_install(app_dir)
    config = data_dir / "config.json"
    game, query = read_ports(config)
    ports = tuple(sorted({game, query}))
    rules = [
        Rule(GAME_RULE, "UDP", ports, str(data_dir.joinpath(*SERVER_EXE))),
        Rule(CHECK_RULE, "UDP", ports, str(app_dir / MANAGER_EXE)),
    ]
    if read_remote_access(config):
        # Reachable from home and work networks only, never from a network Windows marks Public.
        rules.append(Rule(WEB_RULE, "TCP", (web_port,), str(app_dir / MANAGER_EXE), "private,domain"))
    return rules


def _system32(name: str) -> str:
    buffer = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(buffer, 260)
    return os.path.join(buffer.value, name)


def add_args(rule: Rule) -> list[str]:
    """Arguments for netsh, as a list: nothing here is ever interpreted by a shell."""
    return [
        "advfirewall", "firewall", "add", "rule",
        f"name={rule.name}", "dir=in", "action=allow", "enable=yes",
        f"protocol={rule.protocol}",
        f"localport={','.join(str(p) for p in rule.ports)}",
        f"program={rule.program}",
        f"profile={rule.profiles}",
    ]


def delete_args(name: str) -> list[str]:
    return ["advfirewall", "firewall", "delete", "rule", f"name={name}"]


def _netsh(args: list[str]) -> int:
    return subprocess.run(
        [_system32("netsh.exe"), *args], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
    ).returncode


def sync(app_dir: Path) -> int:
    """Make the firewall match the settings. Returns a process exit code; reports nothing else."""
    try:
        rules = desired_rules(app_dir)
    except FirewallError:
        return 2  # settings unreadable or invalid: leave the firewall exactly as it is
    for name in (*RULES, *LEGACY_RULES):
        _netsh(delete_args(name))  # "no such rule" is fine
    failed = [rule.name for rule in rules if _netsh(add_args(rule)) != 0]
    return 1 if failed else 0


def remove_all() -> None:
    for name in (*RULES, *LEGACY_RULES):
        _netsh(delete_args(name))


# ---------- read-only view, used by the manager's health check (needs no privileges) ----------

_QUERY = (
    "$r = Get-NetFirewallRule -DisplayName '{name}' -ErrorAction SilentlyContinue | Select-Object -First 1;"
    "if (-not $r) {{ 'null'; exit 0 }};"
    "$p = $r | Get-NetFirewallPortFilter; $a = $r | Get-NetFirewallApplicationFilter;"
    "@{{ enabled = ($r.Enabled -eq 'True'); direction = [string]$r.Direction; action = [string]$r.Action;"
    " protocol = [string]$p.Protocol; ports = @($p.LocalPort | ForEach-Object {{ [string]$_ }}); program = [string]$a.Program }}"
    " | ConvertTo-Json -Compress"
)


def parse_rule(text: str) -> dict | None:
    """Turn the query's JSON into {protocol, ports, program} or None when there is no usable rule."""
    try:
        data = json.loads(text.strip() or "null")
    except ValueError:
        return None
    if not isinstance(data, dict) or not data.get("enabled") or data.get("direction") != "Inbound" or data.get("action") != "Allow":
        return None
    ports = []
    for item in data.get("ports") or []:
        for part in str(item).split(","):
            if part.strip().isdigit():
                ports.append(int(part))
    return {"protocol": str(data.get("protocol", "")).upper(), "ports": tuple(sorted(ports)), "program": str(data.get("program", ""))}


def current_rule(name: str) -> dict | None:
    if name not in RULES:
        raise ValueError(name)
    try:
        result = subprocess.run(
            [_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive", "-Command", _QUERY.format(name=name)],
            capture_output=True, text=True, timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return parse_rule(result.stdout)


def in_sync(current: dict | None, wanted: Rule) -> bool:
    return bool(
        current
        and current["protocol"] == wanted.protocol
        and current["ports"] == wanted.ports
        # Windows paths whatever runs this code: the comparison is also tested on Linux.
        and ntpath.normcase(current["program"]) == ntpath.normcase(wanted.program)
    )
