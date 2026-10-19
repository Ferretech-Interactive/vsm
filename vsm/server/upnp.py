"""Optional UPnP: ask the home router to forward the game ports to this machine.

Standard library, plus psutil to list network adapters. Everything the router says is treated as untrusted input from the
local network: responses are size-capped and parsed without entity expansion, and the
control URL must be on the private address that actually answered discovery, so a rogue
reply cannot point the manager at some other host.

Only the two game ports (UDP) are ever mapped. The manager's web port is never forwarded.
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import select
import socket
import struct
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit
from xml.sax.saxutils import escape

import psutil

log = logging.getLogger("vsm.upnp")

SSDP_ADDRESS = ("239.255.255.250", 1900)
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
)
SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)
MAX_RESPONSE = 256 * 1024
TIMEOUT = 5
LEASE = 3600  # seconds; renewed well before it runs out
RENEW_EVERY = 900
LONG_LEASE = 24 * 3600  # used when the manager stops but the game keeps running
DESCRIPTION = "Vein Server Manager"
ONLY_PERMANENT = 725
CONFLICT = 718
CGNAT = ipaddress.ip_network("100.64.0.0/10")


class UpnpError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a router has no reason to redirect; following one could leave the LAN


_opener = urllib.request.build_opener(_NoRedirect)


def _private_host(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def parse_ssdp(data: bytes) -> str | None:
    """The LOCATION header of a discovery reply."""
    for line in data.decode("latin-1", errors="replace").split("\r\n")[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "location":
            return value.strip()
    return None


def check_url(url: str, responder: str) -> str:
    """Accept only plain-HTTP URLs on the private address that answered discovery."""
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname != responder or not _private_host(responder):
        raise UpnpError("The router's reply pointed somewhere other than itself; ignoring it.")
    return url


def _http(url: str, data: bytes | None = None, headers: dict | None = None) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with _opener.open(request, timeout=TIMEOUT) as response:
            body = response.read(MAX_RESPONSE + 1)
    except urllib.error.HTTPError as e:
        body = e.read(MAX_RESPONSE + 1)
        if len(body) > MAX_RESPONSE:
            raise UpnpError("The router sent an oversized reply.")
        match = re.search(rb"<errorCode>\s*(\d+)\s*</errorCode>", body)
        if match:
            raise UpnpError(f"The router refused the request (UPnP error {int(match.group(1))}).", int(match.group(1)))
        raise UpnpError(f"The router answered HTTP {e.code}.")
    except (OSError, ValueError) as e:
        raise UpnpError(f"Could not talk to the router: {e}")
    if len(body) > MAX_RESPONSE:
        raise UpnpError("The router sent an oversized reply.")
    return body


def _parse_xml(body: bytes) -> ET.Element:
    # Only plain ASCII/UTF-8 XML: another encoding (UTF-16) would hide a DOCTYPE from the check below.
    if not body.removeprefix(b"\xef\xbb\xbf").lstrip().startswith(b"<"):
        raise UpnpError("The router sent a reply that is not valid XML.")
    if b"<!DOCTYPE" in body or b"<!ENTITY" in body:
        raise UpnpError("The router sent a document with entity definitions; refusing to parse it.")
    try:
        return ET.fromstring(body)
    except ET.ParseError:
        raise UpnpError("The router sent a reply that is not valid XML.")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_description(body: bytes, location: str, responder: str) -> tuple[str, str]:
    """Find the WAN connection service in the router's description: (service type, control URL)."""
    root = _parse_xml(body)
    base = location
    for element in root.iter():
        if _local(element.tag) == "URLBase" and element.text:
            base = element.text.strip()
    found: dict[str, str] = {}
    for service in root.iter():
        if _local(service.tag) != "service":
            continue
        fields = {_local(child.tag): (child.text or "").strip() for child in service}
        if fields.get("serviceType") in SERVICE_TYPES and fields.get("controlURL"):
            found[fields["serviceType"]] = fields["controlURL"]
    for service_type in SERVICE_TYPES:
        if service_type in found:
            return service_type, check_url(urljoin(base, found[service_type]), responder)
    raise UpnpError("The router does not offer port forwarding over UPnP.")


def soap_body(service_type: str, action: str, arguments: dict) -> bytes:
    args = "".join(f"<{name}>{escape(str(value))}</{name}>" for name, value in arguments.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        f'<s:Body><u:{action} xmlns:u="{service_type}">{args}</u:{action}></s:Body></s:Envelope>'
    ).encode()


def is_double_nat(address: str) -> bool:
    """True when the router's "external" address is not a public one (double NAT or carrier-grade NAT)."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.is_private or ip in CGNAT or not ip.is_global


class Gateway:
    def __init__(self, service_type: str, control_url: str, local_ip: str):
        self.service_type = service_type
        self.control_url = control_url
        self.local_ip = local_ip
        self.responder = ""  # the address that answered discovery
        self.adapter = ""  # set when the person chose the adapter: then they are told which device answered

    def _call(self, action: str, arguments: dict) -> dict:
        body = _http(
            self.control_url, soap_body(self.service_type, action, arguments),
            {"Content-Type": 'text/xml; charset="utf-8"', "SOAPAction": f'"{self.service_type}#{action}"'},
        )
        return {_local(e.tag): (e.text or "") for e in _parse_xml(body).iter() if _local(e.tag).startswith("New")}

    def external_ip(self) -> str:
        return self._call("GetExternalIPAddress", {}).get("NewExternalIPAddress", "")

    def add(self, port: int, lease: int) -> None:
        self._call("AddPortMapping", {
            "NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP", "NewInternalPort": port,
            "NewInternalClient": self.local_ip, "NewEnabled": 1, "NewPortMappingDescription": DESCRIPTION,
            "NewLeaseDuration": lease,
        })

    def delete(self, port: int) -> None:
        self._call("DeletePortMapping", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP"})

    def lookup(self, port: int) -> dict:
        return self._call("GetSpecificPortMappingEntry", {"NewRemoteHost": "", "NewExternalPort": port, "NewProtocol": "UDP"})


def adapters() -> list[dict]:
    """This machine's network adapters that are up and have an IPv4 address (not loopback or
    link-local): name, address and netmask."""
    found: list[dict] = []
    try:
        stats = psutil.net_if_stats()
        for name, addresses in psutil.net_if_addrs().items():
            if not (stats.get(name) and stats[name].isup):
                continue
            for a in addresses:
                if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                    found.append({"name": name, "address": a.address, "netmask": a.netmask or "255.255.255.255"})
    except Exception:  # noqa: BLE001 - discovery still works from the default adapter
        log.debug("could not list network adapters", exc_info=True)
    return found


def local_addresses() -> list[str]:
    return [a["address"] for a in adapters()]


def looking_at(adapter: str = "") -> dict:
    """Where discovery will look. Automatic (no adapter named): the adapter internet traffic
    leaves through, and that network's router. A named adapter: that adapter and its network."""
    known = adapters()
    if adapter:
        chosen = next((a for a in known if a["name"] == adapter), None)
        if not chosen:
            raise UpnpError(f"The network adapter chosen for UPnP ({adapter}) is not connected. Choose another under Server Settings.")
        network = ipaddress.ip_interface(f"{chosen['address']}/{chosen['netmask']}").network
        return {"name": adapter, "address": chosen["address"], "gateway": None, "network": network, "chosen": True}
    address = outbound_address()
    name = next((a["name"] for a in known if a["address"] == address), "")
    return {"name": name, "address": address, "gateway": default_gateway() if address else None, "network": None, "chosen": False}


def not_found(where: dict) -> str:
    """Why no router was found, saying where the manager looked and where else it could."""
    if where["chosen"]:
        text = f"No UPnP device answered on {where['name']} ({where['address']})."
    elif where["address"]:
        on = f"{where['name']} ({where['address']})" if where["name"] else where["address"]
        text = f"No UPnP router answered on {on}" + (f"; this network's router is {where['gateway']}." if where["gateway"] else ".")
    else:
        text = "No UPnP router was found on this network."
    text += " It may be turned off in the router's settings."
    others = [f"{a['name']} ({a['address']})" for a in adapters() if a["address"] != where["address"]]
    if others:
        text += " If your router is on a different network adapter, choose that adapter instead: " + ", ".join(others[:6]) + "."
    return text


def outbound_address() -> str | None:
    """This machine's address on the adapter its internet traffic leaves through, if it has one."""
    try:
        return _local_ip_towards("1.1.1.1")  # only chooses a route; nothing is sent
    except OSError:
        return None


def default_gateway() -> str | None:
    """The router this machine sends its internet traffic to, as the operating system has it."""
    try:
        if os.name == "nt":
            import ctypes

            row = (ctypes.c_uint32 * 14)()  # MIB_IPFORWARDROW: fourteen DWORDs, the fourth is the next hop
            destination = struct.unpack("<I", socket.inet_aton("1.1.1.1"))[0]
            if ctypes.windll.iphlpapi.GetBestRoute(destination, 0, ctypes.byref(row)) != 0:
                return None
            hop = socket.inet_ntoa(struct.pack("<I", row[3]))
        else:
            hop = None
            with open("/proc/net/route", encoding="ascii") as routes:
                for line in routes.readlines()[1:]:
                    fields = line.split()
                    if len(fields) > 3 and fields[1] == "00000000" and int(fields[3], 16) & 2:  # default route, via a gateway
                        hop = socket.inet_ntoa(struct.pack("<I", int(fields[2], 16)))
                        break
        return hop if hop and hop != "0.0.0.0" else None
    except Exception:  # noqa: BLE001 - not knowing is handled by the caller
        log.debug("could not read the default route", exc_info=True)
        return None


def from_own_router(replies: list[tuple[str, str]], gateway: str | None) -> list[tuple[str, str]]:
    """Keep only answers from this machine's own router.

    Any device on the network may answer a discovery request and call itself an internet
    gateway. Only the router this machine really sends its traffic through can forward ports
    to it, so when that router is known, nobody else's answer is used.
    """
    return [reply for reply in replies if reply[1] == gateway] if gateway else replies


def search(timeout: float = 3, address: str | None = None) -> list[tuple[str, str]]:
    """Broadcast a discovery request; returns (description URL, responder address) pairs.

    The request is sent from the adapter this machine reaches the internet through, not from
    wherever the system would send multicast. A PC with virtual adapters (VMware, Hyper-V, WSL,
    a VPN) often prefers one of those, and a request sent there never reaches the real router.
    Only when that adapter cannot be determined is every adapter tried. `address` sends from
    one particular adapter instead (the one chosen in the settings).
    """
    sockets: list[socket.socket] = []
    replies: list[tuple[str, str]] = []
    outbound = address or outbound_address()
    try:
        for address in [outbound] if outbound else [None, *local_addresses()]:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                if address:
                    sock.bind((address, 0))
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(address))
                for target in SEARCH_TARGETS:
                    message = (
                        "M-SEARCH * HTTP/1.1\r\n"
                        f"HOST: {SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}\r\n"
                        'MAN: "ssdp:discover"\r\nMX: 2\r\n'
                        f"ST: {target}\r\n\r\n"
                    )
                    sock.sendto(message.encode(), SSDP_ADDRESS)
                sockets.append(sock)
            except OSError as e:
                sock.close()  # an adapter that cannot send (no route, being torn down) is skipped
                log.debug("discovery could not be sent from %s: %s", address or "the default adapter", e)
        if not sockets:
            raise UpnpError("Could not search the network for a router: no network adapter could send the request.")
        deadline = time.time() + timeout
        while len(replies) < 16:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            ready, _, _ = select.select(sockets, [], [], remaining)
            for sock in ready:
                try:
                    data, (address, _port) = sock.recvfrom(4096)
                except OSError:
                    continue
                location = parse_ssdp(data)
                if location and (location, address) not in replies:
                    replies.append((location, address))
    finally:
        for sock in sockets:
            sock.close()
    return replies


def _local_ip_towards(address: str) -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((address, 1900))  # no packet is sent; this only picks the outgoing interface
        return probe.getsockname()[0]
    finally:
        probe.close()


def find_gateway(replies: list[tuple[str, str]] | None = None, adapter: str = "") -> Gateway:
    """Find the router to ask. `adapter` is the one named in the settings, or "" for automatic.

    Automatic believes only this machine's own default gateway. With a named adapter the
    gateway of that network is not known to us, so any device on that adapter's own network is
    accepted, and the caller says which device it was so the person can see what they trust.
    """
    where = None
    if replies is None:
        where = looking_at(adapter)
        answers = search(address=where["address"])
        if where["chosen"]:
            replies = [r for r in answers if _in_network(r[1], where["network"])]
        else:
            replies = from_own_router(answers, where["gateway"])
        if answers and not replies:
            log.info("ignored UPnP answers from %s: not the router of the network being asked", sorted({a for _, a in answers}))
    problem: UpnpError | None = None
    for location, responder in replies:
        try:
            body = _http(check_url(location, responder))
            service_type, control = parse_description(body, location, responder)
            gateway = Gateway(service_type, control, _local_ip_towards(responder))
            gateway.responder = responder
            gateway.adapter = where["name"] if where and where["chosen"] else ""
            return gateway
        except UpnpError as e:
            problem = e
    raise problem or UpnpError(not_found(where) if where else "No UPnP router was found on this network. It may be turned off in the router's settings.")


def _in_network(address: str, network) -> bool:
    try:
        return ipaddress.ip_address(address) in network
    except ValueError:
        return False


class Upnp:
    """Keeps the game ports mapped while the game server runs."""

    def __init__(self, manager):
        self.m = manager
        self.available = not os.path.exists("/.dockerenv")  # a container cannot see the home router
        self._gateway: Gateway | None = None
        self._ports: tuple[int, ...] = ()
        self._permanent = False
        self._renew_at = 0.0
        self._lock = threading.Lock()
        self._status = {"state": "off", "message": "", "external_ip": "", "ports": []}

    def status(self) -> dict:
        if not self.available:
            return {"state": "unavailable", "message": "Not available inside Docker; forward the ports on your router yourself.", "external_ip": "", "ports": []}
        if not self.m.config.get().network.upnp and self._status["state"] != "mapped":
            return {"state": "off", "message": "", "external_ip": "", "ports": []}
        return dict(self._status)

    def _set(self, state: str, message: str, external_ip: str = "", ports=()) -> None:
        changed = (state, message) != (self._status["state"], self._status["message"])
        self._status = {"state": state, "message": message, "external_ip": external_ip, "ports": list(ports)}
        if changed and message:
            self.m.event("UPNP", message)

    def _map(self, gateway: Gateway, ports: tuple[int, ...], lease: int) -> bool:
        """Map every port; returns True when the router insisted on permanent mappings."""
        permanent = False
        for port in ports:
            try:
                gateway.add(port, 0 if permanent else lease)
            except UpnpError as e:
                if e.code != ONLY_PERMANENT:
                    raise
                permanent = True  # this router only does mappings that never expire; removed on stop
                gateway.add(port, 0)
        return permanent

    def open(self) -> None:
        """Called after the game server starts. Never blocks the caller."""
        if self.available and self.m.config.get().network.upnp:
            threading.Thread(target=self._open, name="upnp", daemon=True).start()

    def _open(self) -> None:
        launch = self.m.config.get().launch
        ports = tuple(sorted({launch.port, launch.query_port}))  # the game ports and nothing else
        with self._lock:
            try:
                gateway = find_gateway(adapter=self.m.config.get().network.upnp_adapter)
                self._permanent = self._map(gateway, ports, LEASE)
                external = gateway.external_ip()
            except UpnpError as e:
                hint = " Another device already uses that port on the router." if e.code == CONFLICT else ""
                self._set("error", f"Router port forwarding failed: {e}{hint}")
                return
            self._gateway, self._ports, self._renew_at = gateway, ports, time.time() + RENEW_EVERY
            if is_double_nat(external):
                self._set("mapped", f"Ports {', '.join(map(str, ports))} are forwarded, but the router's own address ({external}) is not a public one. "
                                    "There is another router or your provider's NAT in front of it, so players may still be unable to connect.", external, ports)
            else:
                who = f"The device at {gateway.responder} (on {gateway.adapter})" if gateway.adapter else "Router"
                self._set("mapped", f"{who} is forwarding UDP {', '.join(map(str, ports))} to this machine.", external, ports)

    def tick(self) -> None:
        """Called regularly by the scheduler: renew the lease before it expires."""
        if not self._gateway or self._permanent or time.time() < self._renew_at:
            return
        with self._lock:
            if not self._gateway:
                return
            try:
                self._map(self._gateway, self._ports, LEASE)
                self._renew_at = time.time() + RENEW_EVERY
            except UpnpError as e:
                self._set("error", f"Could not renew router port forwarding: {e}")
                self._gateway = None

    def close(self) -> None:
        """The game server has stopped: remove the mappings."""
        with self._lock:
            gateway, ports = self._gateway, self._ports
            self._gateway, self._ports = None, ()
            if not gateway:
                return
            for port in ports:
                try:
                    gateway.delete(port)
                except UpnpError as e:
                    log.warning("could not remove UPnP mapping for %s: %s", port, e)
            self._set("off", "Router port forwarding removed.")

    def extend(self) -> None:
        """The manager is stopping but the game keeps running: keep players connected for a day."""
        with self._lock:
            if self._gateway and not self._permanent:
                try:
                    self._map(self._gateway, self._ports, LONG_LEASE)
                except UpnpError as e:
                    log.warning("could not extend UPnP mappings: %s", e)
