import time

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        yield c


def stub(client, monkeypatch, download_ok=True):
    """Replace the slow or external parts: SteamCMD, UE4SS download and launching the game."""
    m = client.app.state.manager
    calls = []

    def update(validate=False):
        calls.append("download")
        if download_ok:
            m.paths.server_exe.parent.mkdir(parents=True, exist_ok=True)
            m.paths.server_exe.write_bytes(b"exe")
        return download_ok

    monkeypatch.setattr(m.steam, "update", update)
    monkeypatch.setattr(m.ue4ss, "install_from_github", lambda: calls.append("ue4ss"))
    monkeypatch.setattr(m.server, "_do_start", lambda trigger: calls.append(f"start:{trigger}"))
    m.ue4ss.supported = True
    return m, calls


def wait_idle(client):
    for _ in range(100):
        s = client.get("/api/status").json()
        if not s["task"]:
            return s
        time.sleep(0.05)
    pytest.fail("setup did not finish")


PLAN = {
    "config": {"launch": {"port": 7779, "query_port": 27016, "max_players": 24}, "behavior": {"auto_start": True}},
    "game": {"server_name": "Guided", "super_admins": ["76561198000000001"]},
    "ue4ss": True,
    "start": True,
}


def test_needs_setup_until_installed(client):
    assert client.get("/api/status").json()["setup"]["needed"] is True
    m = client.app.state.manager
    m.paths.server_exe.parent.mkdir(parents=True)
    m.paths.server_exe.write_bytes(b"exe")
    assert client.get("/api/status").json()["setup"]["needed"] is False  # an existing server never triggers it


def test_setup_runs_every_step_in_order(client, monkeypatch):
    m, calls = stub(client, monkeypatch)
    assert client.post("/api/setup", json=PLAN).status_code == 200
    s = wait_idle(client)
    assert calls == ["download", "ue4ss", "start:SETUP"]
    assert [step["state"] for step in s["setup"]["steps"]] == ["done"] * 4 and s["setup"]["error"] is None
    assert s["setup"]["needed"] is False
    assert m.config.get().launch.port == 7779 and m.config.get().behavior.auto_start is True
    assert client.get("/api/game-settings").json()["server_name"] == "Guided"


def test_setup_validates_everything_before_starting(client, monkeypatch):
    m, calls = stub(client, monkeypatch)
    bad = [
        {**PLAN, "config": {"launch": {"port": 80}}},
        {**PLAN, "config": {"launch": {"port": "abc"}}},
        {**PLAN, "config": {"discord": {"bot_token": "x"}}},  # not something setup may touch
        {**PLAN, "game": {"super_admins": ["123"]}},
        {**PLAN, "game": {"password": "has space"}},
    ]
    for plan in bad:
        assert client.post("/api/setup", json=plan).status_code == 400
    assert calls == [] and m.config.get().launch.port == 7777
    assert client.get("/api/game-settings").json()["server_name"] != "Guided"


def test_failed_download_stops_the_rest_and_can_be_retried(client, monkeypatch):
    m, calls = stub(client, monkeypatch, download_ok=False)
    client.post("/api/setup", json=PLAN)
    s = wait_idle(client)
    assert calls == ["download"]
    assert [step["state"] for step in s["setup"]["steps"]] == ["done", "failed", "pending", "pending"]
    assert "Downloading the game server failed" in s["setup"]["error"] and s["state"] == "OFFLINE"

    calls.clear()
    stub(client, monkeypatch, download_ok=True)[1]
    m2, calls2 = stub(client, monkeypatch, download_ok=True)
    client.post("/api/setup", json=PLAN)
    s = wait_idle(client)
    assert calls2 == ["download", "ue4ss", "start:SETUP"] and s["setup"]["error"] is None


def test_setup_can_choose_the_first_admin_password(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("VSM_LOCAL_LOGIN", "1")
    from vsm.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as c:
        c.post("/api/auth/local", json={"token": (tmp_path / "desktop.token").read_text()})
        stub(c, monkeypatch)
        remote = {**PLAN, "config": {"network": {"remote_access": True}}}
        assert c.post("/api/setup", json=remote).status_code == 400  # other devices need a password first
        assert c.post("/api/setup", json={**remote, "admin_password": "short"}).status_code == 400
        assert c.post("/api/setup", json={**remote, "admin_password": PASSWORD}).status_code == 200
        wait_idle(c)
        assert c.get("/api/auth/state").json()["needs_password"] is False
        assert c.get("/api/config").json()["network"]["remote_access"] is True
        # once chosen, setup cannot be used to replace it
        assert c.post("/api/setup", json={**PLAN, "admin_password": "another-password"}).status_code == 400
        # but trying again after a failure resends the same password, and that is accepted
        assert c.post("/api/setup", json={**remote, "admin_password": PASSWORD}).status_code == 200
        wait_idle(c)


def test_setup_is_refused_while_the_server_runs(client, monkeypatch):
    m, calls = stub(client, monkeypatch)
    before = client.get("/api/config").json()["launch"]["port"]
    m.server.pid = 4242  # a game server is running
    try:
        response = client.post("/api/setup", json={**PLAN, "config": {"launch": {"port": before + 1}}})
    finally:
        m.server.pid = None
    assert response.status_code == 409
    assert calls == [] and client.get("/api/config").json()["launch"]["port"] == before  # nothing was half-applied


def test_remote_access_setting_needs_a_password(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("VSM_LOCAL_LOGIN", "1")
    from vsm.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as c:
        c.post("/api/auth/local", json={"token": (tmp_path / "desktop.token").read_text()})
        assert c.put("/api/config", json={"network": {"remote_access": True}}).status_code == 400
        c.post("/api/auth/password", json={"new": PASSWORD})
        assert c.put("/api/config", json={"network": {"remote_access": True}}).status_code == 200


def test_setup_is_logged(client, monkeypatch):
    m, _calls = stub(client, monkeypatch)
    plan = {**PLAN, "game": {**PLAN["game"], "password": "secretjoin"}}
    client.post("/api/setup", json=plan)
    wait_idle(client)
    events = [e["message"] for e in m.hub.snapshot("event") if e["category"] == "SETUP"]
    assert events[0].startswith("Guided setup started: Saving your settings, Downloading the game server")
    assert any(e.startswith('Settings saved: server "Guided", ports 7779/27016 UDP, 24 players') and "join password set" in e for e in events)
    assert sum(": done (" in e for e in events) == 4 and events[-1].startswith("Guided setup finished in")
    log = (m.paths.logs / "manager.log").read_text(encoding="utf-8")
    assert "Guided setup started" in log and "secretjoin" not in log and PASSWORD not in log

    stub(client, monkeypatch, download_ok=False)
    client.post("/api/setup", json=PLAN)
    wait_idle(client)
    errors = [e["message"] for e in m.hub.snapshot("event") if e["category"] == "ERROR"]
    assert any("Downloading the game server failed" in e for e in errors)


def test_steamcmd_output_is_kept_on_disk(client):
    m = client.app.state.manager
    m.steam._out("Update state (0x61) downloading, progress: 42.00")
    assert "progress: 42.00" in (m.paths.logs / "steamcmd.log").read_text(encoding="utf-8")
    assert "progress: 42.00" not in (m.paths.logs / "manager.log").read_text(encoding="utf-8")
