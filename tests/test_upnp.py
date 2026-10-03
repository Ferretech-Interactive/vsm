import http.server
import re
import threading

import pytest

from vsm.server import upnp
from vsm.server.upnp import UpnpError

SERVICE = "urn:schemas-upnp-org:service:WANIPConnection:1"
REAL_FIND_GATEWAY = upnp.find_gateway
DESCRIPTION = f"""<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
  <device>
    <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
    <serviceList><service><serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType><controlURL>/l3f</controlURL></service></serviceList>
    <deviceList><device><deviceList><device>
      <serviceList><service><serviceType>{SERVICE}</serviceType><controlURL>/ctl/IPConn</controlURL></service></serviceList>
    </device></deviceList></device></deviceList>
  </device>
</root>"""


class FakeRouter:
    """A minimal Internet Gateway Device: description document plus the SOAP control endpoint."""

    def __init__(self, external_ip="198.51.100.7", only_permanent=False, taken=()):
        self.external_ip, self.only_permanent, self.taken = external_ip, only_permanent, set(taken)
        self.mappings: dict[int, dict] = {}
        self.requests: list[tuple[str, str, str]] = []
        self.description = DESCRIPTION
        router = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, code, body):
                data = body.encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._reply(200, router.description)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                action = self.headers["SOAPAction"].strip('"').split("#")[1]
                router.requests.append((action, self.headers["SOAPAction"], body))
                fields = dict(re.findall(r"<(New\w+)>([^<]*)</", body))
                port = int(fields.get("NewExternalPort", 0))
                if action == "GetExternalIPAddress":
                    return self._reply(200, f"<r><NewExternalIPAddress>{router.external_ip}</NewExternalIPAddress></r>")
                if action == "AddPortMapping":
                    if port in router.taken:
                        return self._reply(500, "<e><errorCode>718</errorCode></e>")
                    if router.only_permanent and fields["NewLeaseDuration"] != "0":
                        return self._reply(500, "<e><errorCode>725</errorCode></e>")
                    router.mappings[port] = fields
                    return self._reply(200, "<r/>")
                if action == "DeletePortMapping":
                    router.mappings.pop(port, None)
                    return self._reply(200, "<r/>")
                self._reply(500, "<e><errorCode>401</errorCode></e>")

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.location = f"http://127.0.0.1:{self.server.server_port}/rootDesc.xml"
        self.replies = [(self.location, "127.0.0.1")]

    def close(self):
        self.server.shutdown()


@pytest.fixture
def router():
    r = FakeRouter()
    yield r
    r.close()


def test_parse_ssdp():
    reply = b"HTTP/1.1 200 OK\r\nCACHE-CONTROL: max-age=120\r\nLocation:  http://192.168.1.1:5000/rootDesc.xml \r\nST: x\r\n\r\n"
    assert upnp.parse_ssdp(reply) == "http://192.168.1.1:5000/rootDesc.xml"
    assert upnp.parse_ssdp(b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n") is None


def test_add_renew_delete_round_trip(router):
    gateway = upnp.find_gateway(router.replies)
    assert gateway.service_type == SERVICE and gateway.control_url.endswith("/ctl/IPConn") and gateway.local_ip == "127.0.0.1"
    gateway.add(7777, 3600)
    gateway.add(7777, 3600)  # renewing is the same request again
    assert router.mappings[7777] == {
        "NewRemoteHost": "", "NewExternalPort": "7777", "NewProtocol": "UDP", "NewInternalPort": "7777",
        "NewInternalClient": "127.0.0.1", "NewEnabled": "1", "NewPortMappingDescription": "Vein Server Manager",
        "NewLeaseDuration": "3600",
    }
    action, header, body = router.requests[-1]
    assert header == f'"{SERVICE}#AddPortMapping"' and f'<u:AddPortMapping xmlns:u="{SERVICE}">' in body
    assert gateway.external_ip() == "198.51.100.7"
    gateway.delete(7777)
    assert router.mappings == {}


def test_router_errors():
    router = FakeRouter(taken={7777})
    try:
        gateway = upnp.find_gateway(router.replies)
        with pytest.raises(UpnpError) as conflict:
            gateway.add(7777, 3600)
        assert conflict.value.code == upnp.CONFLICT
    finally:
        router.close()


@pytest.mark.parametrize("location, responder", [
    ("http://127.0.0.1:{port}/rootDesc.xml", "192.168.1.1"),  # points at a different host than the one that answered
    ("https://127.0.0.1:{port}/rootDesc.xml", "127.0.0.1"),  # not plain http
    ("http://8.8.8.8/rootDesc.xml", "8.8.8.8"),  # a public address is not a home router
    ("http://router.example/rootDesc.xml", "router.example"),  # host names are not accepted
])
def test_untrusted_locations_are_rejected(router, location, responder):
    with pytest.raises(UpnpError):
        upnp.find_gateway([(location.format(port=router.server.server_port), responder)])
    assert router.requests == []


def test_control_url_on_another_host_is_rejected(router):
    router.description = DESCRIPTION.replace("/ctl/IPConn", "http://10.9.9.9/ctl/IPConn")
    with pytest.raises(UpnpError):
        upnp.find_gateway(router.replies)
    router.description = DESCRIPTION.replace("</root>", "<URLBase>http://10.9.9.9:80/</URLBase></root>")
    with pytest.raises(UpnpError):
        upnp.find_gateway(router.replies)


@pytest.mark.parametrize("document", [
    "x" * (upnp.MAX_RESPONSE + 100),
    "<root><unclosed></root>",
    '<!DOCTYPE r [<!ENTITY a "aaaa">]><root>&a;</root>',
    "<root><device/></root>",  # valid, but no port-forwarding service
], ids=["oversize", "malformed", "entities", "no-service"])
def test_bad_descriptions_are_rejected(router, document):
    router.description = document
    with pytest.raises(UpnpError):
        upnp.find_gateway(router.replies)


@pytest.mark.parametrize("address, double", [
    ("8.8.8.8", False), ("192.168.0.2", True), ("10.0.0.2", True), ("100.64.1.1", True), ("100.127.255.254", True), ("0.0.0.0", True), ("", False),
])
def test_double_nat_detection(address, double):
    assert upnp.is_double_nat(address) is double


# ---------- the manager component ----------

class FakeManager:
    def __init__(self, enabled=True, port=7777, query=27015):
        import types

        self.events = []
        cfg = types.SimpleNamespace(network=types.SimpleNamespace(upnp=enabled, upnp_adapter=""), launch=types.SimpleNamespace(port=port, query_port=query))
        self.config = types.SimpleNamespace(get=lambda: cfg)

    def event(self, category, message):
        self.events.append(message)


def component(router, monkeypatch, **kw):
    monkeypatch.setattr(upnp, "find_gateway", lambda replies=None, adapter="": REAL_FIND_GATEWAY(router.replies))
    m = FakeManager(**kw)
    u = upnp.Upnp(m)
    u.available = True
    return m, u


def test_only_the_game_ports_are_mapped_and_removed(router, monkeypatch):
    m, u = component(router, monkeypatch, port=7779, query=27016)
    u._open()
    assert sorted(router.mappings) == [7779, 27016]  # never the web port, whatever else is configured
    assert all(f["NewProtocol"] == "UDP" and f["NewLeaseDuration"] == "3600" for f in router.mappings.values())
    assert u.status()["state"] == "mapped" and u.status()["external_ip"] == "198.51.100.7" and u.status()["ports"] == [7779, 27016]

    u._renew_at = 0
    before = len(router.requests)
    u.tick()
    assert len(router.requests) == before + 2  # one renewal per port

    u.extend()
    assert all(f["NewLeaseDuration"] == str(upnp.LONG_LEASE) for f in router.mappings.values())
    u.close()
    assert router.mappings == {} and u.status()["state"] == "off"


def test_off_by_default_and_unavailable_in_docker(router, monkeypatch):
    m, u = component(router, monkeypatch, enabled=False)
    u.open()
    assert router.requests == [] and u.status()["state"] == "off"
    u.available = False
    assert u.status()["state"] == "unavailable"


def test_permanent_only_router_and_double_nat_and_conflict(monkeypatch):
    router = FakeRouter(external_ip="100.64.3.9", only_permanent=True)
    try:
        m, u = component(router, monkeypatch)
        u._open()
        assert all(f["NewLeaseDuration"] == "0" for f in router.mappings.values()) and u._permanent
        assert "not a public one" in u.status()["message"]
        before = len(router.requests)
        u._renew_at = 0
        u.tick()
        assert len(router.requests) == before  # permanent mappings need no renewal
        u.close()
        assert router.mappings == {}
    finally:
        router.close()

    router = FakeRouter(taken={7777})
    try:
        m, u = component(router, monkeypatch)
        u._open()
        assert u.status()["state"] == "error" and "Another device already uses that port" in u.status()["message"]
    finally:
        router.close()


def test_discovery_is_sent_from_every_adapter(monkeypatch):
    """A PC with virtual adapters may send multicast out of the wrong one; the router must still be asked."""
    import socket
    import threading

    answering = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    answering.bind(("127.0.0.1", 0))
    answering.settimeout(5)
    askers = set()

    def router():
        while True:
            try:
                data, sender = answering.recvfrom(4096)
            except OSError:
                return
            askers.add(sender)
            if b"M-SEARCH" in data:
                answering.sendto(b"HTTP/1.1 200 OK\r\nLOCATION: http://192.168.1.1:5000/desc.xml\r\n\r\n", sender)

    threading.Thread(target=router, daemon=True).start()
    monkeypatch.setattr(upnp, "SSDP_ADDRESS", answering.getsockname())
    monkeypatch.setattr(upnp, "local_addresses", lambda: ["127.0.0.1"])
    monkeypatch.setattr(upnp, "outbound_address", lambda: None)  # the internet-facing adapter is not known
    try:
        replies = upnp.search(timeout=1)
        assert replies == [("http://192.168.1.1:5000/desc.xml", "127.0.0.1")]  # the same answer twice counts once
        assert len(askers) == 2  # asked once the default way and once from the adapter's own address
        # When the internet-facing adapter is known, the request goes out of that one only.
        askers.clear()
        monkeypatch.setattr(upnp, "outbound_address", lambda: "127.0.0.1")
        monkeypatch.setattr(upnp, "local_addresses", lambda: pytest.fail("the other adapters must not be used"))
        assert len(upnp.search(timeout=1)) == 1 and len(askers) == 1
    finally:
        answering.close()


def test_discovery_survives_an_adapter_that_cannot_send(monkeypatch):
    monkeypatch.setattr(upnp, "outbound_address", lambda: None)
    monkeypatch.setattr(upnp, "local_addresses", lambda: ["203.0.113.250"])  # not an address of this machine
    monkeypatch.setattr(upnp, "SSDP_ADDRESS", ("127.0.0.1", 9))
    assert upnp.search(timeout=0.3) == []


def test_only_this_machines_own_router_is_believed(monkeypatch):
    router_answer = ("http://192.168.1.1:5000/desc.xml", "192.168.1.1")
    other_device = ("http://192.168.1.77:49152/desc.xml", "192.168.1.77")  # anything may call itself a gateway
    assert upnp.from_own_router([other_device, router_answer], "192.168.1.1") == [router_answer]
    assert upnp.from_own_router([other_device], "192.168.1.1") == []
    assert upnp.from_own_router([other_device, router_answer], None) == [other_device, router_answer]  # route unknown: as before
    # A search that only hears from some other device finds no router, and never contacts that device.
    monkeypatch.setattr(upnp, "search", lambda timeout=3, address=None: [other_device])
    monkeypatch.setattr(upnp, "default_gateway", lambda: "192.168.1.1")
    monkeypatch.setattr(upnp, "_http", lambda *a, **k: pytest.fail("a device that is not the router must not be contacted"))
    with pytest.raises(UpnpError, match="No UPnP router"):
        upnp.find_gateway()


def test_default_gateway_is_an_address_or_unknown():
    import ipaddress

    gateway = upnp.default_gateway()
    assert gateway is None or ipaddress.ip_address(gateway).version == 4


ETHERNET = {"name": "Ethernet", "address": "192.168.1.162", "netmask": "255.255.255.0"}
VPN = {"name": "Work VPN", "address": "10.8.0.6", "netmask": "255.255.255.0"}


def test_a_failed_search_says_where_it_looked_and_what_else_there_is(monkeypatch):
    monkeypatch.setattr(upnp, "adapters", lambda: [VPN, ETHERNET])
    monkeypatch.setattr(upnp, "outbound_address", lambda: "10.8.0.6")  # a VPN that takes all traffic
    monkeypatch.setattr(upnp, "default_gateway", lambda: "10.8.0.1")
    asked = []
    monkeypatch.setattr(upnp, "search", lambda timeout=3, address=None: asked.append(address) or [])
    with pytest.raises(UpnpError) as failure:
        upnp.find_gateway()
    message = str(failure.value)
    assert asked == ["10.8.0.6"]
    assert "No UPnP router answered on Work VPN (10.8.0.6); this network's router is 10.8.0.1." in message
    assert "choose that adapter instead: Ethernet (192.168.1.162)." in message


def test_a_chosen_adapter_is_asked_and_only_its_own_network_is_believed(monkeypatch, router):
    monkeypatch.setattr(upnp, "adapters", lambda: [VPN, ETHERNET])
    monkeypatch.setattr(upnp, "outbound_address", lambda: pytest.fail("automatic detection must not be used"))
    monkeypatch.setattr(upnp, "default_gateway", lambda: pytest.fail("automatic detection must not be used"))
    asked = []
    outsider = ("http://10.8.0.1:5000/desc.xml", "10.8.0.1")  # answers, but is not on the chosen adapter's network
    monkeypatch.setattr(upnp, "search", lambda timeout=3, address=None: asked.append(address) or [outsider])
    monkeypatch.setattr(upnp, "_http", lambda *a, **k: pytest.fail("a device on another network must not be contacted"))
    with pytest.raises(UpnpError, match=r"No UPnP device answered on Ethernet \(192.168.1.162\)"):
        upnp.find_gateway(adapter="Ethernet")
    assert asked == ["192.168.1.162"]
    with pytest.raises(UpnpError, match="is not connected"):
        upnp.find_gateway(adapter="Wi-Fi")


def test_a_device_found_on_a_chosen_adapter_is_named(monkeypatch, router):
    loopback = {"name": "Test adapter", "address": "127.0.0.1", "netmask": "255.0.0.0"}
    monkeypatch.setattr(upnp, "adapters", lambda: [loopback])
    monkeypatch.setattr(upnp, "search", lambda timeout=3, address=None: router.replies)
    gateway = upnp.find_gateway(adapter="Test adapter")
    assert (gateway.responder, gateway.adapter) == ("127.0.0.1", "Test adapter")


def test_adapter_setting_and_list(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from vsm.app import create_app

    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(upnp, "adapters", lambda: [VPN, ETHERNET])
    monkeypatch.setattr(upnp, "outbound_address", lambda: "10.8.0.6")
    monkeypatch.setattr(upnp, "default_gateway", lambda: "10.8.0.1")
    with TestClient(create_app()) as client:
        assert client.get("/api/upnp/adapters").status_code == 401
        client.post("/api/auth/login", json={"password": "test-password"})
        assert client.get("/api/upnp/adapters").json() == {
            "adapters": [{"name": "Work VPN", "address": "10.8.0.6"}, {"name": "Ethernet", "address": "192.168.1.162"}],
            "automatic": {"name": "Work VPN", "address": "10.8.0.6", "gateway": "10.8.0.1"},
        }
        # Looking for the router from the page: an answer either way, and nothing is forwarded.
        monkeypatch.setattr(upnp, "search", lambda timeout=3, address=None: [])
        assert client.post("/api/upnp/check").status_code in (400, 422)
        missed = client.post("/api/upnp/check", json={"adapter": ""}).json()
        assert missed["ok"] is False and "No UPnP router answered on Work VPN" in missed["message"] and "Ethernet (192.168.1.162)" in missed["message"]
        assert "is not connected" in client.post("/api/upnp/check", json={"adapter": "Wi-Fi"}).json()["message"]

        class Found:
            responder = "192.168.1.1"

            def external_ip(self):
                return "100.64.0.9"  # a carrier's shared address, not a public one

            def add(self, *a):
                pytest.fail("looking must not forward anything")

        monkeypatch.setattr(upnp, "find_gateway", lambda replies=None, adapter="": Found())
        assert client.post("/api/upnp/check", json={"adapter": "Ethernet"}).json() == {"ok": True, "device": "192.168.1.1", "double_nat": True, "message": ""}
        assert client.get("/api/config").json()["network"]["upnp_adapter"] == ""
        assert client.put("/api/config", json={"network": {"upnp_adapter": "Ethernet"}}).status_code == 200
        assert client.put("/api/config", json={"network": {"upnp_adapter": "x" * 101}}).status_code == 400
        assert client.put("/api/config", json={"network": {"upnp_adapter": "bad\nname"}}).status_code == 400
