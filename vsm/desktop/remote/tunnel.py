"""A port on this PC's loopback address that leads to a deployed server's manager, through SSH.

The manager on such a server listens on its own loopback address only. The app signs in to the
server as the `vsm-tunnel` account with that server's single-purpose key and asks SSH to forward
each local connection to the manager's port. The server must present the host key saved when it
was deployed; a different key means no connection.
"""
from __future__ import annotations

import logging
import select
import socket
import threading
import time

import paramiko

from .ssh import Session, SignInRefused, SshError, Target

log = logging.getLogger("vsm.desktop.remote")

TUNNEL_USER = "vsm-tunnel"
RETRY_AFTER = 5  # seconds between attempts to sign in again after the connection drops


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tunnel:
    def __init__(self, host: str, ssh_port: int, host_key: str, key: paramiko.PKey, remote_port: int, local_port: int):
        self.target = Target(host, ssh_port, TUNNEL_USER)
        self.host_key = host_key
        self.key = key
        self.remote_port = remote_port
        self.local_port = local_port
        self.error: str | None = None
        self._session: Session | None = None
        self._listener: socket.socket | None = None
        self._lock = threading.Lock()
        self._last_attempt = 0.0
        self._stopped = threading.Event()

    @property
    def up(self) -> bool:
        transport = self._session.client.get_transport() if self._session else None
        return bool(transport and transport.is_active())

    def start(self, check: bool = True) -> None:
        """Sign in and start listening. Raises SshError if the server cannot be reached now, or
        (with `check`) if it will not carry a connection to the manager: better said once here,
        in words, than left as a page that fails to load."""
        self._connect()
        if check:
            try:
                self._channel(("127.0.0.1", 0)).close()
            except SshError:
                self._session.close()
                raise
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.1", self.local_port))  # this PC only, never the network
        except OSError:
            listener.close()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))  # the saved port is taken: use another one this time
            self.local_port = listener.getsockname()[1]
        listener.listen(32)
        self._listener = listener
        threading.Thread(target=self._accept, name="ssh-tunnel", daemon=True).start()

    def stop(self) -> None:
        self._stopped.set()
        if self._listener:
            try:
                self._listener.close()
            except OSError:
                pass
        if self._session:
            self._session.close()

    def _connect(self) -> None:
        with self._lock:
            if self.up:
                return
            self._last_attempt = time.monotonic()
            if self._session:
                self._session.close()
            try:
                self._session = Session(self.target, self.host_key, pkey=self.key)
                self.error = None
            except SignInRefused as e:
                # Nobody typed this key or user name, so "check them" would be no help.
                self.error = (f"{self.target.host} no longer accepts this app's key. It may have been removed from the "
                              f"{TUNNEL_USER} account, or the server was reinstalled. Deploy to the server again to repair it.")
                raise SshError(self.error) from e
            except SshError as e:
                self.error = str(e)
                raise

    def _channel(self, peer):
        try:
            return self._session.client.get_transport().open_channel("direct-tcpip", ("127.0.0.1", self.remote_port), peer, timeout=15)
        except paramiko.ChannelException as e:
            if e.code == 1:  # administratively prohibited
                self.error = ("The server refused to forward the connection. Its SSH service has port forwarding turned off "
                              "(AllowTcpForwarding), or this app's key was removed from the vsm-tunnel account. Deploy to the server again to repair it.")
            else:
                self.error = "The manager on the server is not answering. It may be stopped: check it on the server (systemctl status vsm, or docker ps)."
            raise SshError(self.error) from e

    def _accept(self) -> None:
        while not self._stopped.is_set():
            try:
                client, peer = self._listener.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._serve, args=(client, peer), name="ssh-tunnel-conn", daemon=True).start()

    def _serve(self, client: socket.socket, peer) -> None:
        channel = None
        try:
            if not self.up:
                if time.monotonic() - self._last_attempt < RETRY_AFTER:
                    return  # just failed: do not hammer the server for every request the page makes
                self._connect()
            channel = self._channel(peer)
            while True:
                ready, _, _ = select.select([client, channel], [], [], 60)
                if client in ready:
                    data = client.recv(65536)
                    if not data:
                        break
                    channel.sendall(data)
                if channel in ready:
                    data = channel.recv(65536)
                    if not data:
                        break
                    client.sendall(data)
                if not ready and (channel.closed or self._stopped.is_set()):
                    break
        except (SshError, paramiko.SSHException, OSError, EOFError) as e:
            if not isinstance(e, SshError):
                self.error = f"The connection through SSH failed: {e}"
            log.info("tunnel connection ended: %s", e)
        finally:
            for end in (channel, client):
                try:
                    if end is not None:
                        end.close()
                except Exception:  # noqa: BLE001
                    pass
