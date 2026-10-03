"""One SSH connection to a Linux server, with the rules the rest of the package relies on.

- The server's host key is always known before anything is sent to it: `fetch_host_key` shows it
  to the person first, and `Session` only connects to a server presenting exactly that key.
- Commands are fixed argument lists, quoted here. Nothing typed by a person is ever part of a
  shell line; host, port and user are validated before use.
- Passwords go to the server over the encrypted channel only: as the SSH password, or on a
  command's standard input (sudo). Never on a command line, never in a log.
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import shlex
import socket
from dataclasses import dataclass
from pathlib import Path

import paramiko

log = logging.getLogger("vsm.desktop.remote")
# Failures are turned into SshError and shown to the person; paramiko's own tracebacks for the
# same events (a server that hangs up, a refused channel) would only bury that in the log.
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

_HOST = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9.\-]*[A-Za-z0-9])?$")
_IPV6 = re.compile(r"^[0-9A-Fa-f:]{2,39}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,31}$")
_KEY_LINE = re.compile(r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)) ([A-Za-z0-9+/]+={0,2})$")
CONNECT_TIMEOUT = 15
MAX_LINES = 400


class SshError(Exception):
    """A failure worth showing to the person as it is."""


class SignInRefused(SshError):
    """The server answered, and is the right server, but did not accept the password or key."""


@dataclass(frozen=True)
class Target:
    host: str
    port: int
    user: str

    @property
    def label(self) -> str:
        return f"{self.user}@{self.host}" + ("" if self.port == 22 else f":{self.port}")


def target(host: object, port: object, user: object) -> Target:
    host, user = str(host or "").strip(), str(user or "").strip()
    if not (_HOST.match(host) or _IPV6.match(host)):
        raise SshError("Enter the server's name or IP address, for example 192.168.1.50 or vein.example.com.")
    try:
        port = 22 if port is None or port == "" else int(port)
    except (TypeError, ValueError):
        port = 0
    if not 1 <= port <= 65535:
        raise SshError("The SSH port must be a number from 1 to 65535 (usually 22).")
    if not _USER.match(user):
        raise SshError("Enter the Linux user name to sign in as.")
    return Target(host, port, user)


def key_line(key: paramiko.PKey) -> str:
    """A host key as stored and compared: 'type base64'."""
    return f"{key.get_name()} {key.get_base64()}"


def parse_key_line(line: str) -> paramiko.PKey:
    m = _KEY_LINE.match(str(line).strip())
    if not m:
        raise SshError("The saved host key for this server is not usable.")
    try:
        return paramiko.PKey.from_type_string(m.group(1), base64.b64decode(m.group(2), validate=True))
    except (binascii.Error, paramiko.SSHException, ValueError) as e:
        raise SshError("The saved host key for this server is not usable.") from e


def fingerprint(line: str) -> str:
    """The SHA256 fingerprint as `ssh-keygen -lf` prints it."""
    return parse_key_line(line).fingerprint


def fetch_host_key(to: Target) -> str:
    """Ask the server who it is, and nothing else: no sign-in is attempted."""
    try:
        sock = socket.create_connection((to.host, to.port), timeout=CONNECT_TIMEOUT)
    except OSError as e:
        raise SshError(f"Could not reach {to.host} on port {to.port}: {e.strerror or e}.") from e
    transport = paramiko.Transport(sock)
    try:
        transport.start_client(timeout=CONNECT_TIMEOUT)
        return key_line(transport.get_remote_server_key())
    except (paramiko.SSHException, OSError, EOFError) as e:
        raise SshError(f"{to.host} did not answer as an SSH server: {e}") from e
    finally:
        transport.close()


def load_key_file(path: str, passphrase: str | None) -> paramiko.PKey:
    """A private key file chosen by the person, with messages that say what is wrong with it."""
    if not Path(path).is_file():
        raise SshError("That private key file does not exist.")
    try:
        # Positional, and as bytes: the parameter's name differs between paramiko versions.
        return paramiko.PKey.from_path(path, passphrase.encode() if passphrase else None)
    except paramiko.PasswordRequiredException as e:
        raise SshError("That private key is protected by a passphrase: enter it.") from e
    except (paramiko.SSHException, ValueError, TypeError, OSError) as e:
        if not passphrase and ("password" in str(e).lower() or "encrypted" in str(e).lower()):
            raise SshError("That private key is protected by a passphrase: enter it.") from e
        if passphrase:
            raise SshError("That private key could not be opened: the passphrase is wrong, or the file is not a private key.") from e
        raise SshError("That file is not a private key this app can use (OpenSSH format: ed25519, ECDSA or RSA).") from e


def _known_host_name(to: Target) -> str:
    return to.host if to.port == 22 else f"[{to.host}]:{to.port}"


class Session:
    """A signed-in connection to a server whose host key equals `host_key` (a `key_line`)."""

    def __init__(self, to: Target, host_key: str, *, password: str | None = None, key_path: str | None = None,
                 passphrase: str | None = None, agent: bool = False, pkey: paramiko.PKey | None = None):
        self.target = to
        self.client = paramiko.SSHClient()
        expected = parse_key_line(host_key)
        self.client.get_host_keys().add(_known_host_name(to), expected.get_name(), expected)
        self.client.set_missing_host_key_policy(paramiko.RejectPolicy())  # a key we were not shown is refused
        if key_path:
            pkey = load_key_file(key_path, passphrase)  # read here, so a bad file is not reported as a bad connection
        try:
            self.client.connect(
                to.host, port=to.port, username=to.user, password=password or None, pkey=pkey,
                allow_agent=agent, look_for_keys=False,
                timeout=CONNECT_TIMEOUT, banner_timeout=CONNECT_TIMEOUT, auth_timeout=CONNECT_TIMEOUT,
            )
        except paramiko.BadHostKeyException as e:
            raise SshError(
                f"{to.host} is presenting a different host key from the one saved for it. That happens when the server "
                "was reinstalled, and also when something is impersonating it. Nothing was sent. If you know why it "
                "changed, remove the server from the list and add it again."
            ) from e
        except paramiko.PasswordRequiredException as e:
            raise SshError("That private key is protected by a passphrase: enter it.") from e
        except paramiko.AuthenticationException as e:
            raise SignInRefused(f"{to.host} refused the sign-in for {to.user}. Check the user name and the password or key.") from e
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise SshError(f"Could not connect to {to.host}: {e}") from e
        self.client.get_transport().set_keepalive(30)

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:  # noqa: BLE001 - closing must not fail
            pass

    def run(self, argv: list[str], stdin: bytes | None = None, on_line=None, timeout: float | None = 120) -> tuple[int, str]:
        """Run one command given as arguments. Returns (exit code, output); standard error is
        merged in. `stdin` is sent and then closed, which is how a secret reaches a command."""
        command = shlex.join(argv)
        try:
            channel = self.client.get_transport().open_session(timeout=CONNECT_TIMEOUT)
            channel.settimeout(timeout)
            channel.set_combine_stderr(True)
            channel.exec_command(command)
            if stdin is not None:
                channel.sendall(stdin)
            channel.shutdown_write()
            lines: list[str] = []
            pending = b""
            while True:
                data = channel.recv(65536)
                if not data:
                    break
                pending += data
                *whole, pending = pending.split(b"\n")
                for raw in whole:
                    self._line(raw, lines, on_line)
            if pending:
                self._line(pending, lines, on_line)
            return channel.recv_exit_status(), "\n".join(lines)
        except socket.timeout as e:
            raise SshError(f"The server stopped answering while running {argv[0]}.") from e
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise SshError(f"The connection to {self.target.host} was lost: {e}") from e

    @staticmethod
    def _line(raw: bytes, lines: list[str], on_line) -> None:
        text = _ANSI.sub("", raw.decode("utf-8", "replace")).rstrip("\r")
        lines.append(text)
        del lines[:-MAX_LINES]
        if on_line:
            on_line(text)

    def upload(self, data: bytes, remote: str, mode: int = 0o600) -> None:
        """Write a file. The permissions are set before any content is sent."""
        try:
            sftp = self.client.open_sftp()
            try:
                with sftp.open(remote, "wb") as f:
                    sftp.chmod(remote, mode)
                    f.set_pipelined(True)
                    f.write(data)
            finally:
                sftp.close()
        except (paramiko.SSHException, OSError, EOFError) as e:
            raise SshError(f"Could not copy a file to {self.target.host}: {e}") from e


_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


class Root:
    """How this session becomes root: it already is, sudo asks nothing, or sudo takes a password."""

    def __init__(self, session: Session, sudo_password: str | None):
        self.session = session
        self.prefix: list[str] = []
        self.stdin: bytes | None = None
        code, out = session.run(["id", "-u"])
        if code == 0 and out.strip() == "0":
            return
        if session.run(["sudo", "-n", "true"])[0] == 0:
            self.prefix = ["sudo", "-n"]
            return
        if not sudo_password:
            raise SshError(f"{session.target.user} needs a password to use sudo on this server: enter it.")
        secret = sudo_password.encode("utf-8") + b"\n"
        # -k: do not rely on an earlier sudo; -S: read the password from standard input; -p '': no prompt text.
        code, out = session.run(["sudo", "-k", "-S", "-p", "", "true"], stdin=secret)
        if code != 0:
            raise SshError(f"sudo refused: either the password is wrong or {session.target.user} may not use sudo on this server.")
        self.prefix, self.stdin = ["sudo", "-k", "-S", "-p", ""], secret

    def run(self, argv: list[str], on_line=None, timeout: float | None = 120) -> tuple[int, str]:
        return self.session.run(self.prefix + argv, stdin=self.stdin, on_line=on_line, timeout=timeout)
