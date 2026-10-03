import socket
import threading

import pytest
from fastapi.testclient import TestClient

from vsm import VsmError
from vsm.app import create_app
from vsm.server import portcheck


def free_udp_ports(count=2):
    sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(count)]
    for s in sockets:
        s.bind(("127.0.0.1", 0))
    ports = [s.getsockname()[1] for s in sockets]
    for s in sockets:
        s.close()
    return [p for p in ports if p >= 1024]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(portcheck, "WAIT", 1.0)
    monkeypatch.setattr(portcheck, "local_ip", lambda: "192.168.1.20")
    with TestClient(create_app()) as client:
        client.post("/api/auth/login", json={"password": "test-password"})
        yield client, client.app.state.manager


def fake_probe(monkeypatch, send_to=None, payload=None, error=None):
    """Stands in for the Port Probe Service: sends the packets itself, over loopback."""
    seen = {}

    def ask(url, ports, nonce):
        seen.update(url=url, ports=ports, nonce=nonce)
        if error:
            raise error

        def send():
            out = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            for port in ports if send_to is None else send_to:
                out.sendto(payload if payload is not None else portcheck.MAGIC + nonce.encode(), ("127.0.0.1", port))
            out.close()

        threading.Thread(target=send).start()
        return {"ip": "203.0.113.7", "ports": ports}

    monkeypatch.setattr(portcheck, "ask_probe", ask)
    return seen


def test_reachable_ports_and_what_is_sent(setup, monkeypatch):
    client, m = setup
    game, query = free_udp_ports()
    seen = fake_probe(monkeypatch)
    result = client.post("/api/portcheck", json={"port": game, "query_port": query}).json()
    assert result["reachable"] is True and [p["reachable"] for p in result["ports"]] == [True, True]
    assert result["public_ip"] == "203.0.113.7" and result["local_ip"] == "192.168.1.20" and result["behind_router"] is True
    assert result["upnp"] is None and result["firewall_ready"] is True
    # Only the two ports and a random nonce go to the configured service.
    assert seen["url"] == "https://be1.ferretech.org/v1/probe" and seen["ports"] == [game, query] and len(seen["nonce"]) == 32
    # The ports that were checked are the ones now saved.
    launch = m.config.get().launch
    assert (launch.port, launch.query_port) == (game, query)
    # The ports are free again afterwards.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("0.0.0.0", game))
    s.close()


def test_one_port_closed_and_foreign_packets_ignored(setup, monkeypatch):
    client, _m = setup
    game, query = free_udp_ports()
    fake_probe(monkeypatch, send_to=[game])
    result = client.post("/api/portcheck", json={"port": game, "query_port": query}).json()
    assert result["reachable"] is False and [p["reachable"] for p in result["ports"]] == [True, False]

    fake_probe(monkeypatch, payload=b"PORT-PROBE1 0000000000000000")  # someone else's packet
    result = client.post("/api/portcheck", json={"port": game, "query_port": query}).json()
    assert [p["reachable"] for p in result["ports"]] == [False, False]


def test_refusals(setup, monkeypatch):
    client, m = setup
    game, query = free_udp_ports()
    called = fake_probe(monkeypatch)
    assert client.post("/api/portcheck", json={"port": 80, "query_port": query}).status_code == 422
    assert client.post("/api/portcheck", json={"port": game, "query_port": game}).status_code == 400

    busy = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    busy.bind(("0.0.0.0", query))
    try:
        r = client.post("/api/portcheck", json={"port": game, "query_port": query})
        assert r.status_code == 409 and "in use" in r.json()["detail"]
    finally:
        busy.close()

    monkeypatch.setattr(m.server, "running", lambda: True)
    r = client.post("/api/portcheck", json={"port": game, "query_port": query})
    assert r.status_code == 409 and "Stop the game server" in r.json()["detail"]
    monkeypatch.setattr(m.server, "running", lambda: False)

    client.put("/api/config", json={"network": {"probe_url": ""}})
    r = client.post("/api/portcheck", json={"port": game, "query_port": query})
    assert r.status_code == 409 and "turned off" in r.json()["detail"]
    assert called == {}  # none of the refusals contacted the service


def test_service_errors_are_reported(setup, monkeypatch):
    client, _m = setup
    game, query = free_udp_ports()
    fake_probe(monkeypatch, error=VsmError("The check service is limiting how often this address may ask. Wait a minute and try again.", 429))
    r = client.post("/api/portcheck", json={"port": game, "query_port": query})
    assert r.status_code == 429 and "Wait a minute" in r.json()["detail"]


def test_probe_address_must_be_https(setup):
    client, _m = setup
    assert client.put("/api/config", json={"network": {"probe_url": "http://example.com/v1/probe"}}).status_code == 400
    assert client.put("/api/config", json={"network": {"probe_url": "https://probe.example.com/v1/probe"}}).status_code == 200


def test_temporary_upnp_mapping_is_removed(setup, monkeypatch):
    client, _m = setup
    game, query = free_udp_ports()
    fake_probe(monkeypatch)
    calls = []

    class Gateway:
        def add(self, port, lease):
            calls.append(("add", port, lease))

        def delete(self, port):
            calls.append(("delete", port))

        def external_ip(self):
            return "100.64.0.5"  # carrier-grade NAT

    monkeypatch.setattr(portcheck.upnp, "find_gateway", lambda adapter="": Gateway())
    result = client.post("/api/portcheck", json={"port": game, "query_port": query, "upnp": True}).json()
    assert result["upnp"] == {"ok": True, "message": "", "double_nat": True}
    assert calls == [("add", game, 120), ("add", query, 120), ("delete", game), ("delete", query)]
