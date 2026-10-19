"""Single-admin authentication: scrypt password hash, in-memory sessions, optional API token.

The desktop app adds a second way in: a per-start token on disk that only a
loopback caller can exchange for a session (see `local_login`).
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from pathlib import Path

from fastapi import Request, WebSocket

from .. import VsmError

COOKIE = "vsm_session"
SESSION_TTL = 7 * 24 * 3600
INITIAL_PASSWORD_FILE = "initial-password"
MIN_PASSWORD = 8
MAX_FAILURES = 5
LOCKOUT = 300
LOOPBACK = {"127.0.0.1", "::1"}
LOCAL_CODE_TTL = 30
MAX_LOCAL_CODES = 64
FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")
MIN_API_TOKEN = 32
# The portable app's sign-in token, in a folder only this Windows user can open (`enable_local_login`).
LOCAL_TOKEN = ("desktop-auth", "desktop.token")

log = logging.getLogger("vsm.auth")


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)


class Auth:
    def __init__(self, path: Path):
        self.path = path
        self.api_token = os.environ.get("VSM_API_TOKEN") or None
        if self.api_token and len(self.api_token) < MIN_API_TOKEN:
            log.warning("VSM_API_TOKEN is shorter than %d characters and is ignored.", MIN_API_TOKEN)
            self.api_token = None
        self._sessions: dict[str, float] = {}
        self._local_sessions: set[str] = set()
        self._local_token: str | None = None
        self._local_codes: dict[str, float] | None = None  # hash -> expiry; None until enabled
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self._record: dict | None = None

    def bootstrap(self) -> str | None:
        """Load the stored hash. Returns a freshly generated password on first run."""
        first = self._take_initial_password()
        env = os.environ.get("VSM_ADMIN_PASSWORD")
        if env:
            self._store(env)  # the environment always wins, so it can be rotated by redeploying
            return None
        try:
            self._record = json.loads(self.path.read_text(encoding="utf-8"))
            return None
        except (OSError, ValueError):
            if first:
                self._store(first)  # chosen by whoever installed this; changeable later, unlike the env one
                return None
            generated = secrets.token_urlsafe(12)
            self._store(generated, generated=True)
            return generated

    def _take_initial_password(self) -> str | None:
        """A first password left beside auth.json by an installer (`install.sh --password-file`).
        The file is deleted whether or not it is used: it only counts when no password exists yet."""
        path = self.path.with_name(INITIAL_PASSWORD_FILE)
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, ValueError):
            return None
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return text if 8 <= len(text) <= 256 else None

    @property
    def needs_password(self) -> bool:
        """True while the password is still the random first-run one nobody has chosen."""
        return bool(self._record and self._record.get("generated"))

    def _store(self, password: str, generated: bool = False) -> None:
        salt = secrets.token_bytes(16)
        self._record = {"salt": salt.hex(), "hash": _hash(password, salt).hex(), "generated": generated}
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self._record), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def verify(self, password: str) -> bool:
        rec = self._record
        if not rec:
            return False
        return hmac.compare_digest(_hash(password, bytes.fromhex(rec["salt"])), bytes.fromhex(rec["hash"]))

    # ---------- sessions ----------

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def export_sessions(self) -> dict:
        """Sessions as stored: hashes of the cookies, never the cookies. Used to keep people
        signed in when the manager restarts in place (core/selfupdate.py)."""
        return {"sessions": dict(self._sessions), "local": sorted(self._local_sessions)}

    def import_sessions(self, data: object) -> None:
        if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
            return
        now = time.time()
        for key, expires in data["sessions"].items():
            if isinstance(key, str) and len(key) == 64 and isinstance(expires, (int, float)) and now < expires <= now + SESSION_TTL:
                self._sessions[key] = float(expires)
        local = data.get("local") if isinstance(data.get("local"), list) else []
        self._local_sessions |= {k for k in local if isinstance(k, str) and k in self._sessions}

    def _new_session(self, local: bool = False) -> str:
        now = time.time()
        self._sessions = {k: exp for k, exp in self._sessions.items() if exp > now}
        self._local_sessions &= self._sessions.keys()
        token = secrets.token_urlsafe(32)
        self._sessions[self._key(token)] = now + SESSION_TTL
        if local:
            self._local_sessions.add(self._key(token))
        return token

    @staticmethod
    def _bucket(ip: str) -> str:
        """Failures are counted per IPv4 address and per IPv6 /64: one machine usually owns a whole /64."""
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return ip
        if address.version == 6:
            if address.ipv4_mapped:
                return str(address.ipv4_mapped)
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
        return str(address)

    def _recent_failures(self, ip: str) -> list[float]:
        now = time.time()
        # Forget old entries, so addresses that come and go cannot grow this without limit.
        self._failures = {k: kept for k, v in self._failures.items() if (kept := [t for t in v if now - t < LOCKOUT])}
        return self._failures.get(self._bucket(ip), [])

    def _check_lockout(self, ip: str) -> list[float]:
        recent = self._recent_failures(ip)
        if len(recent) >= MAX_FAILURES:
            raise VsmError("Too many failed attempts. Try again in a few minutes.", 429)
        return recent

    def _fail(self, ip: str, recent: list[float]) -> None:
        self._failures[self._bucket(ip)] = recent + [time.time()]

    def login(self, ip: str, password: str) -> str:
        with self._lock:
            recent = self._check_lockout(ip)
            if not self.verify(password):
                self._fail(ip, recent)
                raise VsmError("Incorrect password.", 401)
            self._failures.pop(self._bucket(ip), None)
            return self._new_session()

    def logout(self, token: str | None) -> None:
        if token:
            self._sessions.pop(self._key(token), None)
            self._local_sessions.discard(self._key(token))

    def authenticated(self, conn: Request | WebSocket) -> bool:
        token = conn.cookies.get(COOKIE)
        if token and self._sessions.get(self._key(token), 0) > time.time():
            return True
        header = conn.headers.get("authorization", "")
        if self.api_token and header.startswith("Bearer "):
            ip = conn.client.host if conn.client else ""
            with self._lock:
                # A wrong token counts like a wrong password, so the token cannot be guessed either.
                recent = self._recent_failures(ip)
                if len(recent) >= MAX_FAILURES:
                    return False
                if hmac.compare_digest(header[7:].encode(), self.api_token.encode()):
                    return True
                self._fail(ip, recent)
        return False

    # ---------- desktop app sign-in ----------

    def enable_local_login(self, token_file: Path) -> None:
        """Write a fresh token only this OS user can read; the desktop window trades it for a session.

        The token goes into a folder that is made private first, so the file is private from the
        moment it exists (restricting the file after creating it leaves a moment in which another
        account could open it). If the folder cannot be made private, local sign-in stays off and
        the window asks for the password instead."""
        folder = token_file.parent
        try:
            folder.mkdir(mode=0o700, exist_ok=True)
            if os.name == "nt":
                _private_folder_windows(folder)
            else:
                os.chmod(folder, 0o700)
            for old in folder.iterdir():
                old.unlink(missing_ok=True)
            token = secrets.token_urlsafe(32)
            fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as f:
                f.write(token)
        except OSError as e:
            log.error("Local sign-in is off: its token could not be stored privately (%s).", e)
            return
        self._local_token = token

    def enable_local_codes(self) -> None:
        """Service mode: accept one-time codes handed out over the administrator pipe."""
        self._local_codes = {}

    def issue_local_code(self) -> str:
        """A single-use code that expires in seconds. Called only for callers already vetted."""
        code = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock:
            codes = {k: exp for k, exp in (self._local_codes or {}).items() if exp > now}
            while len(codes) >= MAX_LOCAL_CODES:
                codes.pop(min(codes, key=codes.get))
            codes[self._key(code)] = now + LOCAL_CODE_TTL
            self._local_codes = codes
        return code

    def local_login(self, conn: Request, token: str) -> str:
        ip = conn.client.host if conn.client else ""
        # A reverse proxy on this machine makes every remote user look like loopback,
        # so any forwarding header disqualifies the request.
        enabled = self._local_token is not None or self._local_codes is not None
        if not enabled or ip not in LOOPBACK or any(h in conn.headers for h in FORWARDING_HEADERS):
            raise VsmError("Local sign-in is not available.", 403)
        # No lockout here: these secrets are 256 random bits, and counting failures would let
        # any local program lock the administrator out of password sign-in.
        with self._lock:
            if self._local_token is not None and hmac.compare_digest(token.encode(), self._local_token.encode()):
                return self._new_session(local=True)
            if self._local_codes is not None and self._local_codes.pop(self._key(token), 0) > time.time():
                return self._new_session(local=True)
        raise VsmError("Invalid token.", 401)

    def is_local(self, conn: Request | WebSocket) -> bool:
        token = conn.cookies.get(COOKIE)
        return bool(token) and self._key(token) in self._local_sessions

    def change_password(self, current: str, new: str, session: str | None = None, first_time: bool = False) -> None:
        """`session` is the caller's cookie; a desktop (local) session need not know the current password.

        `first_time` (guided setup) lets a signed-in caller replace the random first-run
        password without retyping it; it only applies while that password is still in place.
        """
        if os.environ.get("VSM_ADMIN_PASSWORD"):
            raise VsmError("The password is set by VSM_ADMIN_PASSWORD; change it there.")
        key = self._key(session) if session else None
        skip_check = key in self._local_sessions or (first_time and self.needs_password)
        if not skip_check and not self.verify(current):
            raise VsmError("Current password is incorrect.", 401)
        if len(new) < MIN_PASSWORD:
            raise VsmError(f"New password must be at least {MIN_PASSWORD} characters.")
        with self._lock:
            self._store(new)
            # Sign out everyone else; the caller keeps their session.
            self._sessions = {k: v for k, v in self._sessions.items() if k == key}
            self._local_sessions &= self._sessions.keys()


def _private_folder_windows(folder: Path) -> None:
    """Only the current Windows user, SYSTEM and administrators may open `folder`. Raises OSError otherwise."""
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    flags = {"capture_output": True, "text": True, "creationflags": subprocess.CREATE_NO_WINDOW}
    out = subprocess.run([str(system32 / "whoami.exe"), "/user", "/fo", "csv", "/nh"], **flags).stdout
    sid = re.search(r'"(S-1-[0-9-]+)"', out or "")
    if not sid:
        raise OSError("could not tell who the current user is")
    result = subprocess.run(
        [str(system32 / "icacls.exe"), str(folder), "/inheritance:r", "/grant:r",
         f"*{sid.group(1)}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F", "/L", "/Q"], **flags)
    if result.returncode != 0:
        raise OSError(f"icacls failed: {(result.stderr or result.stdout).strip()}")


def host_names() -> set[str]:
    """This machine's own names, which a browser may legitimately use to reach the manager."""
    names = {"localhost"}
    try:
        host = socket.gethostname().lower()
        names |= {host, f"{host}.local", socket.getfqdn().lower()}
    except OSError:
        pass
    return {n for n in names if n}


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def host_allowed(host_header: str, extra: set[str] | frozenset[str] = frozenset()) -> bool:
    """Is `host_header` a name this manager answers to?

    Checked on every request against DNS rebinding: a web page can point its own domain at
    127.0.0.1, after which the browser treats the manager as that site and lets the page use the
    API. An IP address cannot be re-pointed, so any IP is fine; a name must be this machine's own,
    *.localhost (the desktop app's tunnels), or listed in network.allowed_hosts."""
    host = (host_header or "").strip().lower()
    if not host:
        return False
    if host.startswith("["):  # [IPv6]:port
        end = host.find("]")
        return end > 0 and _is_ip(host[1:end])
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    if _is_ip(name):
        return True
    name = name.rstrip(".")
    return name == "localhost" or name.endswith(".localhost") or name in host_names() or name in extra


def same_origin(conn: Request | WebSocket) -> bool:
    """Reject cross-site browser requests. Non-browser clients send no Origin header."""
    origin = conn.headers.get("origin")
    if not origin:
        return True
    return origin.split("://", 1)[-1].lower() == conn.headers.get("host", "").lower()
