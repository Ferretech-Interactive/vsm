import time

import pytest
from fastapi.testclient import TestClient

from vsm.app import create_app
from vsm.server import gameupdate
from vsm.server.controller import State


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    with TestClient(create_app()) as client:
        client.post("/api/auth/login", json={"password": "test-password"})
        m = client.app.state.manager
        m.paths.server_exe.parent.mkdir(parents=True)
        m.paths.server_exe.write_bytes(b"exe")
        installed = ["100"]
        monkeypatch.setattr(m.steam, "build_id", lambda: installed[0])
        refreshes = []
        monkeypatch.setattr(m.branches, "refresh", lambda: refreshes.append(1) or True)
        m.branches._data = {"checked_at": time.time(), "branches": [{"name": "public", "build_id": 100}, {"name": "experimental", "build_id": 150}]}
        yield client, m, installed, refreshes


def events(client):
    return [e["message"] for e in client.get("/api/logs").json()["events"] if e["category"] == "UPDATE"]


def test_reports_a_newer_build_of_the_configured_branch(setup):
    client, m, installed, _ = setup
    assert client.get("/api/status").json()["game_update"]["available"] is False
    m.branches._data["branches"][0]["build_id"] = 120  # Steam published a new public build
    status = client.get("/api/status").json()["game_update"]
    assert status == {"available": True, "installed": "100", "latest": "120", "branch": "public", "checked_at": m.branches._data["checked_at"]}
    m.game_updates.tick()
    m.game_updates.tick()
    assert len(events(client)) == 1 and "build 120" in events(client)[0]  # announced once per build
    installed[0] = "120"  # updated
    assert client.get("/api/status").json()["game_update"]["available"] is False
    client.put("/api/config", json={"behavior": {"steam_branch": "experimental"}})  # compares against the chosen branch
    assert client.get("/api/status").json()["game_update"]["latest"] == "150"
    client.put("/api/config", json={"behavior": {"game_update_check": False}})
    assert client.get("/api/status").json()["game_update"]["available"] is False


def test_asks_steam_on_the_configured_interval(setup, monkeypatch):
    client, m, _, refreshes = setup
    clock = [m.game_updates._asked]
    monkeypatch.setattr(gameupdate.time, "time", lambda: clock[0])
    client.put("/api/config", json={"behavior": {"update_check_minutes": 45}})
    clock[0] += 45 * 60 - 5
    m.game_updates.tick()
    assert refreshes == []
    clock[0] += 10
    m.game_updates.tick()
    m.game_updates.tick()
    assert refreshes == [1]  # once per interval
    client.put("/api/config", json={"behavior": {"game_update_check": False}})
    clock[0] += 10 * 3600
    m.game_updates.tick()
    assert refreshes == [1]  # turned off: Steam is not asked at all
    for bad in (0, 9, 1441):  # 10 minutes to 24 hours
        assert client.put("/api/config", json={"behavior": {"update_check_minutes": bad}}).status_code == 400
    assert client.put("/api/config", json={"behavior": {"update_check_minutes": 1440}}).status_code == 200


def test_automatic_update_only_when_online_and_empty(setup, monkeypatch):
    client, m, _, _ = setup
    started = []
    monkeypatch.setattr(m.server, "request_auto_update", lambda: started.append(1))
    m.branches._data["branches"][0]["build_id"] = 120
    m.game_updates.tick()
    assert started == []  # the setting is off by default
    client.put("/api/config", json={"behavior": {"game_update_auto": True}})
    m.game_updates.tick()
    assert started == []  # the server is not running: it updates when it is next started
    m.server.state = State.ONLINE
    m.server.sessions["76561198000000001"] = "Player"
    m.game_updates.tick()
    assert started == []  # somebody is playing
    m.server.sessions.clear()
    m.game_updates.tick()
    m.game_updates.tick()
    assert started == [1]  # once, not on every tick
    m.server.state = State.OFFLINE


def test_auto_update_steps(setup, monkeypatch):
    client, m, _, _ = setup
    calls = []
    monkeypatch.setattr(m.server, "_do_stop", lambda trigger, final=State.OFFLINE: calls.append("stop"))
    monkeypatch.setattr(m.steam, "update", lambda validate=False: calls.append("update") or True)
    monkeypatch.setattr(m.server, "_do_start", lambda trigger: calls.append("start"))
    m.server._do_auto_update()
    assert calls == ["stop", "update", "start"]
    calls.clear()
    m.server.sessions["76561198000000001"] = "Player"  # joined in the moment before it began
    m.server._do_auto_update()
    assert calls == []
    m.server.sessions.clear()
    m.server.state = State.OFFLINE
