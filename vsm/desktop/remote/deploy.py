"""Install (or update) Vein Server Manager on a Linux server over SSH, from the files this app carries.

The steps, each started by the Deploy window:

1. `probe`: ask the server for its host key and show the fingerprint. Nothing is sent yet.
2. `check`: sign in with what the person typed, find out how to become root, and look at the
   server (read-only) to offer Docker, a native service, or the reason neither is possible.
3. `install`: upload the bundled package and `install.sh` to a private temporary folder, run it as
   root, remove the folder, then open the SSH tunnel and wait for the manager to answer.

The SSH password, key passphrase and sudo password exist in this object for one deployment and
are dropped by `close`. They are never written to disk or to a log, and never part of a command.
"""
from __future__ import annotations

import logging
import re
import secrets
import sys
import threading
import urllib.request
from pathlib import Path

from ... import __version__
from . import keys, preflight
from .ssh import Root, Session, SshError, Target, fetch_host_key, fingerprint, target
from .tunnel import Tunnel, free_port

log = logging.getLogger("vsm.desktop.remote")

INSTALL_TIMEOUT = 45 * 60
HEALTH_TIMEOUT = 120
_TEMP = re.compile(r"^/tmp/vsm-deploy\.[A-Za-z0-9]{8}$")
_WHEEL = "vein_server_manager-{version}-py3-none-any.whl"
# name on the server -> where it is in the source tree (a packaged app carries them side by side)
_FILES = {
    "install.sh": "install.sh",
    "vsm.service": "deploy/vsm.service",
    "Dockerfile": "deploy/remote/Dockerfile",
    "docker-compose.yml": "docker-compose.yml",
    "entrypoint.sh": "docker/entrypoint.sh",
}


def bundle() -> dict[str, Path]:
    """The files to upload, by the name they get on the server. Raises SshError if this copy of
    the app does not carry them (running from source without a built package)."""
    wheel = _WHEEL.format(version=__version__)
    if getattr(sys, "frozen", False):
        folder = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "remote"
        files = {name: folder / name for name in _FILES} | {wheel: folder / wheel}
    else:
        root = Path(__file__).resolve().parents[3]
        files = {name: root / source for name, source in _FILES.items()} | {wheel: root / "build" / "release" / wheel}
    missing = [name for name, path in files.items() if not path.is_file()]
    if missing:
        raise SshError("This copy of the app does not include the Linux package (" + ", ".join(missing) + "). Use an installed or portable build, or run packaging\\windows\\build.ps1 first.")
    return files


def available() -> bool:
    try:
        bundle()
        return True
    except SshError:
        return False


class Deployment:
    def __init__(self, known_hosts: dict[str, str] | None = None, existing_key=None):
        """`known_hosts`: 'host:port' -> saved host key, for servers already in the list.
        `existing_key(host, port)`: the tunnel key this PC already has for that server, or None."""
        self.known = known_hosts or {}
        self.existing_key = existing_key or (lambda host, port: None)
        self.target: Target | None = None
        self.host_key: str | None = None
        self.session: Session | None = None
        self.root: Root | None = None
        self.assessment: dict | None = None
        self._auth: dict = {}
        # progress of `install`, polled by the window
        self.state = "idle"  # idle / working / done / failed
        self.step = ""
        self.lines: list[str] = []
        self.error: str | None = None
        self.result: dict | None = None
        self._password: str | None = None

    # ---------- step 1 ----------

    def probe(self, host, port, user) -> dict:
        self.close()
        self.target = target(host, port, user)
        self.host_key = fetch_host_key(self.target)
        saved = self.known.get(f"{self.target.host}:{self.target.port}")
        return {
            "server": self.target.label,
            "type": self.host_key.split()[0],
            "fingerprint": fingerprint(self.host_key),
            "known": saved == self.host_key,
            "changed": bool(saved) and saved != self.host_key,
        }

    # ---------- step 2 ----------

    def check(self, auth: dict) -> dict:
        """`auth`: {"method": "password" | "key" | "agent", "password", "key_path", "passphrase", "sudo_password"}."""
        if not self.target or not self.host_key:
            raise SshError("Start again from the first step.")
        bundle()  # fail now, not after signing in, if there is nothing to install
        method = auth.get("method")
        password = str(auth.get("password") or "") if method == "password" else ""
        if method == "password" and not password:
            raise SshError("Enter the password.")
        if method == "key" and not auth.get("key_path"):
            raise SshError("Choose the private key file.")
        if method not in ("password", "key", "agent"):
            raise SshError("Choose how to sign in.")
        if self.session:
            self.session.close()
        self.session = Session(
            self.target, self.host_key, password=password or None,
            key_path=str(auth.get("key_path") or "") if method == "key" else None,
            passphrase=str(auth.get("passphrase") or "") if method == "key" else None,
            agent=method == "agent",
        )
        self.root = Root(self.session, str(auth.get("sudo_password") or "") or password or None)
        code, out = self.session.run(["sh", "-s"], stdin=preflight.SCRIPT.encode())
        if code != 0:
            raise SshError("The server could not be examined: " + (out.strip().splitlines() or ["no output"])[-1])
        forwarding = preflight.forwarding_allowed(self.root.run(preflight.FORWARDING)[1])
        self.assessment = preflight.assess(preflight.parse(out), forwarding)
        return self.assessment

    # ---------- step 3 ----------

    def install(self, kind: str, on_done) -> None:
        """Start installing in the background. `on_done(result, private_key)` is called on success,
        before the state becomes "done", so the server is saved by the time the window sees it."""
        if not self.session or not self.root or not self.assessment:
            raise SshError("Check the server first.")
        if self.assessment["blocking"] or kind not in ("native", "docker") or not self.assessment[kind]["ok"]:
            raise SshError("That kind of install is not possible on this server.")
        if self.state == "working":
            raise SshError("The installation is already running.")
        self.state, self.step, self.lines, self.error, self.result = "working", "Preparing", [], None, None
        threading.Thread(target=self._install, args=(kind, on_done), name="deploy", daemon=True).start()

    def status(self) -> dict:
        return {"state": self.state, "step": self.step, "lines": self.lines[-200:], "error": self.error, "result": self.result}

    def take_password(self) -> str | None:
        """The admin password generated for a new install. Handed out once."""
        password, self._password = self._password, None
        return password

    def _say(self, step: str) -> None:
        self.step = step
        self.lines.append(f"== {step}")
        log.info("deploy to %s: %s", self.target.host, step)

    def _install(self, kind: str, on_done) -> None:
        session, root = self.session, self.root
        temp = None
        tunnel = None
        try:
            files = bundle()
            fresh = not self.assessment["existing"]
            # Deploying again from this PC keeps the key the server already accepts.
            private = self.existing_key(self.target.host, self.target.port)
            if private:
                public = keys.public_line(private)
            else:
                private, public = keys.generate()
            password = secrets.token_urlsafe(18) if fresh else None

            self._say("Copying the files to the server")
            code, out = session.run(["mktemp", "-d", "/tmp/vsm-deploy.XXXXXXXX"])
            temp = out.strip()
            if code != 0 or not _TEMP.match(temp):
                raise SshError("The server could not create a temporary folder.")
            for name, path in files.items():
                data = path.read_bytes()
                if not name.endswith(".whl"):
                    data = data.replace(b"\r\n", b"\n")  # a Windows checkout must not hand bash CRLF
                session.upload(data, f"{temp}/{name}", 0o600)
            session.upload(public.encode() + b"\n", f"{temp}/tunnel.pub", 0o600)
            command = ["bash", f"{temp}/install.sh", f"--{kind}", "--wheel", f"{temp}/{_WHEEL.format(version=__version__)}",
                       "--bind", "127.0.0.1", "--tunnel-key", f"{temp}/tunnel.pub", "--no-auto-install"]
            if password:
                session.upload(password.encode() + b"\n", f"{temp}/initial-password", 0o600)
                command += ["--password-file", f"{temp}/initial-password"]

            self._say("Installing on the server (this takes a few minutes)" if fresh else "Updating the manager on the server")
            code, out = root.run(command, on_line=self.lines.append, timeout=INSTALL_TIMEOUT)
            if code != 0:
                errors = [line for line in out.splitlines() if "error:" in line]
                raise SshError("The installer on the server stopped: " + (errors[-1].split("error:", 1)[1].strip() if errors else "see the output above."))

            self._say("Connecting to the manager through SSH")
            tunnel = Tunnel(self.target.host, self.target.port, self.host_key, keys.load(private), preflight.WEB_PORT, free_port())
            tunnel.start(check=False)  # the manager may still be starting: _wait_healthy is the check
            self._wait_healthy(tunnel)
            tunnel.stop()
            tunnel = None

            self._password = password
            self.result = {"host": self.target.host, "ssh_port": self.target.port, "host_key": self.host_key,
                           "remote_port": preflight.WEB_PORT, "kind": kind, "fresh": fresh}
            on_done(self.result, private)
            self._say("Done")
            self.state = "done"
        except Exception as e:  # noqa: BLE001 - shown to the person
            self.error = str(e) if isinstance(e, SshError) else f"{type(e).__name__}: {e}"
            self.lines.append(f"!! {self.error}")
            self.state = "failed"
            log.warning("deploy to %s failed at '%s': %s", self.target.host if self.target else "?", self.step, self.error)
        finally:
            if tunnel:
                tunnel.stop()
            if temp and _TEMP.match(temp):
                try:
                    session.run(["rm", "-rf", "--", temp])  # the uploaded password and key go with it
                except SshError:
                    pass

    @staticmethod
    def _wait_healthy(tunnel: Tunnel) -> None:
        import time

        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
        deadline = time.monotonic() + HEALTH_TIMEOUT
        last = "no answer"
        while time.monotonic() < deadline:
            try:
                with opener.open(f"http://127.0.0.1:{tunnel.local_port}/api/health", timeout=5) as r:
                    if r.status == 200:
                        return
            except OSError as e:
                last = str(e)
            time.sleep(2)
        raise SshError(f"The manager was installed but could not be reached through SSH ({tunnel.error or last}). Check that the server's SSH service allows port forwarding.")

    def close(self) -> None:
        """Forget everything typed for this deployment and hang up."""
        if self.session:
            self.session.close()
        self.session = self.root = self.assessment = None
        self._auth = {}
        self._password = None
