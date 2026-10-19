"""The window's saved list of managers on other machines.

Only names and addresses are kept (per Windows user); passwords are typed into the remote
manager's own sign-in page and never stored here.

Two kinds of entry: an address the window opens directly, and a server this app deployed over SSH
(`kind: "ssh"`), whose manager listens on its own loopback address and is reached through a
tunnel (`remote/tunnel.py`). For those the list holds where the server is and the host key it must
present; the tunnel's private key is kept separately, encrypted (`remote/keys.py`).
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
from pathlib import Path
from urllib.parse import urlsplit

LOCAL_ID = "local"
LOCAL_NAME = "This PC"
DEFAULT_PORT = 8810
MAX_SERVERS = 50
_LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".home.arpa", ".localhost")


_SSH_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$|^[0-9A-Fa-f:]{2,39}$")
_SSH_KEY = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)) [A-Za-z0-9+/]+={0,2}$")
_ID = re.compile(r"^[0-9a-f]{6,32}$")


class ServerError(ValueError):
    pass


def tunnel_url(entry: dict) -> str:
    """Where the window finds a deployed server: a name of its own on this PC's loopback address.

    Not 127.0.0.1: cookies belong to a host name whatever the port, so that would hand this PC's
    own manager session to the remote manager, and the other way round. Browsers resolve any
    name ending in .localhost to the loopback address without asking DNS.
    """
    return f"http://vsm-{entry['id']}.localhost:{entry['local_port']}"


def _port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ServerError("bad port")
    return value


def normalise(address: str) -> str:
    """Turn what someone typed ("192.168.1.20", "vein.example.com:9000", "https://...") into a base URL."""
    address = address.strip()
    if "://" not in address:
        address = "http://" + address
    try:
        parts = urlsplit(address)
        port = parts.port
    except ValueError:
        raise ServerError("That address is not valid.")
    if parts.scheme not in ("http", "https"):
        raise ServerError("The address must start with http:// or https://.")
    if not parts.hostname or parts.username or parts.password:
        raise ServerError("Enter a host name or IP address, optionally with a port.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ServerError("Enter only the server's address, without a path.")
    if port is None:
        # A bare address means "the manager's usual port"; an https:// one is behind a proxy on 443.
        port = DEFAULT_PORT if parts.scheme == "http" else 443
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    default = 80 if parts.scheme == "http" else 443
    return f"{parts.scheme}://{host}" + ("" if port == default else f":{port}")


def is_unencrypted_public(url: str) -> bool:
    """True when a password sent to this address would cross the internet in clear text."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        pass
    # A name with no dots, or one in a home-network suffix, is on the local network.
    return "." in host and not host.endswith(_LOCAL_SUFFIXES)


class ServerList:
    def __init__(self, path: Path, local_url: str | None):
        self.path = path
        self.local_url = local_url
        self._servers: list[dict] = []
        self.last: str | None = None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for item in data.get("servers", [])[:MAX_SERVERS]:
                try:
                    self._servers.append(self._clean(item))
                except (KeyError, TypeError, ServerError):
                    continue  # skip an entry that was edited into something unusable
            self.last = data.get("last") if isinstance(data.get("last"), str) else None
        except (OSError, ValueError, AttributeError):
            pass

    @classmethod
    def _clean(cls, item: dict) -> dict:
        """One stored entry, checked: the file is only as trustworthy as whatever last wrote it."""
        if item.get("kind") != "ssh":
            return {"id": str(item["id"]), "name": cls._name(item["name"]), "url": normalise(item["url"])}
        server_id, host, key = str(item["id"]), str(item["host"]), str(item["host_key"])
        if not (_ID.match(server_id) and _SSH_HOST.match(host) and _SSH_KEY.match(key)):
            raise ServerError("bad entry")
        return {"id": server_id, "name": cls._name(item["name"]), "kind": "ssh", "host": host, "ssh_port": _port(item["ssh_port"]),
                "host_key": key, "remote_port": _port(item["remote_port"]), "local_port": _port(item["local_port"])}

    @staticmethod
    def _name(name) -> str:
        name = str(name).strip()
        if not name or len(name) > 60:
            raise ServerError("Give the server a name of up to 60 characters.")
        return name

    def entries(self) -> list[dict]:
        local = [{"id": LOCAL_ID, "name": LOCAL_NAME, "url": self.local_url}] if self.local_url else []
        return local + [dict(s, url=tunnel_url(s)) if s.get("kind") == "ssh" else dict(s) for s in self._servers]

    def get(self, server_id: str) -> dict | None:
        return next((s for s in self.entries() if s["id"] == server_id), None)

    def add(self, name: str, address: str) -> dict:
        if len(self._servers) >= MAX_SERVERS:
            raise ServerError("The list is full; remove a server first.")
        entry = {"id": secrets.token_hex(6), "name": self._name(name), "url": normalise(address)}
        self._servers.append(entry)
        self._save()
        return entry

    def find_ssh(self, host: str, ssh_port: int) -> dict | None:
        return next((dict(s) for s in self._servers if s.get("kind") == "ssh" and s["host"] == host and s["ssh_port"] == ssh_port), None)

    def known_hosts(self) -> dict[str, str]:
        """'host:port' -> the host key saved for it, for servers deployed from this app."""
        return {f"{s['host']}:{s['ssh_port']}": s["host_key"] for s in self._servers if s.get("kind") == "ssh"}

    def save_ssh(self, name: str, host: str, ssh_port: int, host_key: str, remote_port: int, local_port: int) -> dict:
        """Add a deployed server, or refresh the entry for the same host and SSH port."""
        fields = self._clean({"id": "000000", "name": name, "kind": "ssh", "host": host, "ssh_port": ssh_port,
                              "host_key": host_key, "remote_port": remote_port, "local_port": local_port})
        for entry in self._servers:
            if entry.get("kind") == "ssh" and entry["host"] == host and entry["ssh_port"] == ssh_port:
                if entry.get("host_key") != fields["host_key"]:
                    # The pinned key is only ever replaced by forgetting the server and deploying again.
                    raise ServerError("This server's key changed; remove it under Servers first.")
                entry.update(fields, id=entry["id"], local_port=entry["local_port"])  # same origin as before
                self._save()
                return dict(entry)
        if len(self._servers) >= MAX_SERVERS:
            raise ServerError("The list is full; remove a server first.")
        entry = dict(fields, id=secrets.token_hex(6))
        self._servers.append(entry)
        self._save()
        return dict(entry)

    def set_local_port(self, server_id: str, port: int) -> None:
        for entry in self._servers:
            if entry["id"] == server_id and entry.get("kind") == "ssh" and entry["local_port"] != port:
                entry["local_port"] = _port(port)
                self._save()

    def update(self, server_id: str, name: str, address: str) -> dict:
        for entry in self._servers:
            if entry["id"] == server_id:
                if entry.get("kind") == "ssh":
                    entry.update(name=self._name(name))  # where it is was set by the deployment
                else:
                    entry.update(name=self._name(name), url=normalise(address))
                self._save()
                return entry
        raise ServerError("That server is not in the list." if server_id != LOCAL_ID else "This PC cannot be changed.")

    def remove(self, server_id: str) -> None:
        if server_id == LOCAL_ID:
            raise ServerError("This PC cannot be removed.")
        self._servers = [s for s in self._servers if s["id"] != server_id]
        if self.last == server_id:
            self.last = None
        self._save()

    def remember(self, server_id: str) -> None:
        self.last = server_id
        self._save()

    def initial(self) -> dict | None:
        """What to open at start: the last one used, else this PC, else nothing yet."""
        return self.get(self.last or "") or self.get(LOCAL_ID)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"servers": self._servers, "last": self.last}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)
