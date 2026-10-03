"""Check from the outside that the game ports are reachable: is the router forwarding them?

The manager listens on the two game ports and asks a Port Probe Service on the internet to send
a packet to each. The service only ever sends to the address the request came from. It learns
this machine's public address and the two port numbers; nothing else is sent to it.

Only run when the user asks for it. The game server must be stopped, because the check has to
listen on the game ports itself.
"""
from __future__ import annotations

import json
import logging
import secrets
import select
import socket
import threading
import time
import urllib.error
import urllib.request

from .. import VsmError, __version__
from ..core.network import local_ip
from . import upnp

log = logging.getLogger("vsm.portcheck")

MAGIC = b"PORT-PROBE1 "  # what the Port Probe Service puts in front of the nonce
WAIT = 5.0  # seconds to wait for the packets after the service has answered
UPNP_LEASE = 120  # a mapping made only for the check; removed straight after
FIREWALL_WAIT = 20  # seconds for the firewall helper to open the ports for the check
MAX_REPLY = 4096


def ask_probe(url: str, ports: list[int], nonce: str) -> dict:
    """Ask the service to send its packets. Returns its reply; raises VsmError with what went wrong."""
    body = json.dumps({"ports": ports, "nonce": nonce}).encode()
    request = urllib.request.Request(url, body, {"Content-Type": "application/json", "User-Agent": f"VeinServerManager/{__version__}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as r:
            reply = json.loads(r.read(MAX_REPLY))
    except urllib.error.HTTPError as e:
        try:
            detail = str(json.loads(e.read(MAX_REPLY)).get("error", ""))[:200]
        except (ValueError, AttributeError, OSError):
            detail = ""
        if e.code == 429:
            raise VsmError("The check service is limiting how often this address may ask. Wait a minute and try again.", 429)
        raise VsmError(f"The check service refused the request: {detail or e.code}", 502)
    except (OSError, ValueError) as e:
        log.info("port check: could not reach %s: %s", url, e)
        raise VsmError("Could not reach the check service. Check the internet connection and try again.", 502)
    if not isinstance(reply, dict) or not isinstance(reply.get("ip"), str):
        raise VsmError("The check service sent an answer that makes no sense.", 502)
    return reply


class PortCheck:
    def __init__(self, manager):
        self.m = manager
        self._lock = threading.Lock()

    def run(self, port: int, query_port: int, use_upnp: bool, upnp_adapter: str | None = None) -> dict:
        url = self.m.config.get().network.probe_url
        if not url:
            raise VsmError("The outside check is turned off (no check service is set).", 409)
        if self.m.server.running() or self.m.server.task:
            raise VsmError("Stop the game server first: the check has to listen on the game ports itself.", 409)
        if not self._lock.acquire(blocking=False):
            raise VsmError("A check is already running.", 409)
        try:
            return self._run(url, port, query_port, use_upnp, upnp_adapter)
        finally:
            self._lock.release()

    def _save_ports(self, port: int, query_port: int) -> None:
        """Checking ports that are not the saved ones would test the wrong firewall rule."""
        launch = self.m.config.get().launch
        if (launch.port, launch.query_port) != (port, query_port):
            self.m.config.update({"launch": {"port": port, "query_port": query_port}})
            self.m.request_firewall_sync()

    def _wait_for_firewall(self, ports: tuple[int, ...]) -> bool:
        """Installed Windows host: wait until the rule that lets the manager hear the test packets is in place."""
        if not self.m.service_mode:
            return True
        from ..desktop import firewall

        deadline = time.time() + FIREWALL_WAIT
        asked = False
        while time.time() < deadline:
            current = firewall.current_rule(firewall.CHECK_RULE)
            if current and current["protocol"] == "UDP" and current["ports"] == ports:
                return True
            if not asked:
                self.m.request_firewall_sync()  # an install from before this rule existed
                asked = True
            time.sleep(1)
        return False

    def _run(self, url: str, port: int, query_port: int, use_upnp: bool, upnp_adapter: str | None = None) -> dict:
        if port == query_port:
            raise VsmError("The game port and query port must be different.")
        self._save_ports(port, query_port)
        ports = [port, query_port]
        firewall_ready = self._wait_for_firewall(tuple(sorted(ports)))

        sockets: dict[socket.socket, int] = {}
        gateway = None
        result_upnp = None
        try:
            for p in ports:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.bind(("0.0.0.0", p))
                except OSError:
                    s.close()
                    raise VsmError(f"Port {p} is in use by another program on this machine, so it cannot be checked.", 409)
                sockets[s] = p

            if use_upnp and self.m.upnp.available:
                try:
                    adapter = self.m.config.get().network.upnp_adapter if upnp_adapter is None else upnp_adapter
                    gateway = upnp.find_gateway(adapter=adapter)
                    for p in ports:
                        gateway.add(p, UPNP_LEASE)
                    external = gateway.external_ip()
                    result_upnp = {"ok": True, "message": "", "double_nat": upnp.is_double_nat(external)}
                except upnp.UpnpError as e:
                    result_upnp = {"ok": False, "message": str(e), "double_nat": False}

            nonce = secrets.token_hex(16)
            reply = ask_probe(url, ports, nonce)
            expected = MAGIC + nonce.encode()
            reached: set[int] = set()
            deadline = time.monotonic() + WAIT
            while len(reached) < len(ports) and (remaining := deadline - time.monotonic()) > 0:
                ready, _, _ = select.select(list(sockets), [], [], remaining)
                for s in ready:
                    try:
                        data, _sender = s.recvfrom(512)
                    except OSError:
                        continue
                    if data == expected:  # anything else on these ports is not our test
                        reached.add(sockets[s])
        finally:
            for s in sockets:
                s.close()
            if gateway:
                for p in ports:
                    try:
                        gateway.delete(p)  # the real mapping is made when the game server starts
                    except upnp.UpnpError:
                        pass

        public, local = reply["ip"], local_ip()
        outcome = {
            "public_ip": public,
            "local_ip": local,
            "behind_router": local != public,
            "ports": [{"port": port, "name": "game", "reachable": port in reached}, {"port": query_port, "name": "query", "reachable": query_port in reached}],
            "reachable": len(reached) == len(ports),
            "upnp": result_upnp,
            # False only on an installed Windows host whose firewall rule did not appear in time.
            "firewall_ready": firewall_ready,
            "firewall_managed": self.m.service_mode,
        }
        self.m.event("NETWORK", "Outside check: " + ", ".join(f"{p['port']} {'reachable' if p['reachable'] else 'not reachable'}" for p in outcome["ports"]) + ".")
        return outcome
