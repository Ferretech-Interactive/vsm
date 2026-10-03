import os
import types

import pytest
from fastapi.testclient import TestClient

from vsm.app import create_app
from vsm.server import usage as usage_module
from vsm.server.usage import HISTORY, Usage


def nics(sent, received):
    nic = types.SimpleNamespace
    return {"Ethernet": nic(bytes_sent=sent, bytes_recv=received), "Loopback Pseudo-Interface 1": nic(bytes_sent=10**12, bytes_recv=10**12), "lo": nic(bytes_sent=10**12, bytes_recv=10**12)}


def test_network_rate_ignores_loopback_and_counter_resets(monkeypatch):
    clock = [100.0]
    counters = [nics(1000, 5000)]
    monkeypatch.setattr(usage_module.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(usage_module.psutil, "net_io_counters", lambda pernic: counters[0])
    u = Usage()
    u.sample(None)
    assert u.latest()["up"] == 0 and u.latest()["down"] == 0  # the first sample has nothing to compare with

    clock[0], counters[0] = 102.0, nics(1000 + 4000, 5000 + 20000)
    u.sample(None)
    assert (u.latest()["up"], u.latest()["down"]) == (2000, 10000)  # bytes per second over two seconds

    clock[0], counters[0] = 104.0, nics(50, 80)  # adapter reset: counters start again
    u.sample(None)
    assert (u.latest()["up"], u.latest()["down"]) == (0, 0)


def test_process_figures_and_history_cap():
    u = Usage()
    assert u.latest() == {"t": 0, "cpu": 0.0, "ram_mb": 0, "up": 0, "down": 0}
    u.sample(os.getpid())
    u.sample(os.getpid())
    latest = u.latest()
    assert latest["ram_mb"] > 0 and 0 <= latest["cpu"] <= 100 and latest["t"] > 0
    u.sample(2**31 - 2)  # no such process
    assert u.latest()["ram_mb"] == 0 and u.latest()["cpu"] == 0
    u.sample(None)
    for _ in range(HISTORY + 10):
        u.sample(None)
    assert len(u.history()) == HISTORY


@pytest.fixture
def authed(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    with TestClient(create_app()) as client:
        client.post("/api/auth/login", json={"password": "test-password"})
        yield client


def test_status_and_history_route(authed):
    status = authed.get("/api/status").json()
    assert set(status["usage"]) == {"t", "cpu", "ram_mb", "up", "down"} and status["ram_mb"] == status["usage"]["ram_mb"]
    assert isinstance(authed.get("/api/usage").json(), list)
