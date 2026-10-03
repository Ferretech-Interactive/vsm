import pytest
from fastapi.testclient import TestClient

from vsm.api.audit import changes
from vsm.app import create_app

PASSWORD = "test-password"


def test_changes_describe_what_differs_without_secret_values():
    before = {"launch": {"port": 7777, "map": "/Game/A"}, "rcon": {"enabled": False, "password": ""}, "admins": ["1", "2"]}
    after = {"launch": {"port": 7780, "map": "/Game/A"}, "rcon": {"enabled": True, "password": "hunter2secret"}, "admins": ["2", "3"]}
    text = changes(before, after, ("rcon.password",))
    assert text == "launch.port 7777 → 7780, rcon.enabled off → on, rcon.password changed, admins: added 3; removed 1"
    assert "hunter2secret" not in text
    assert changes(before, before) == ""
    assert changes({}, {f"k{n}": n for n in range(20)}).endswith("and 8 more")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    with TestClient(create_app()) as client:
        yield client, client.app.state.manager


def audit_lines(m):
    return [e["message"] for e in m.hub.snapshot("event") if e["category"] == "AUDIT"]


def test_sign_in_and_changes_are_audited(setup):
    client, m = setup
    assert client.post("/api/auth/login", json={"password": "wrong-password"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    client.put("/api/config", json={"launch": {"port": 7790}, "rcon": {"enabled": True, "password": "rcon-secret-1"}})
    client.put("/api/config", json={"launch": {"port": 7790}})  # nothing changes: no line
    client.put("/api/game-settings", json={"server_name": "Audited", "password": "joinsecret"})
    client.post("/api/bans", json={"steam_id": "76561198000000001"})
    client.post("/api/profiles", json={"name": "weekend"})
    name = client.post("/api/backups").json()["name"]
    client.delete(f"/api/backups/{name}")
    client.post("/api/auth/logout")

    lines = audit_lines(m)
    assert lines[0] == "Failed sign-in: wrong password (by testclient)"
    assert lines[1] == "Signed in with the admin password (by testclient)"
    assert lines[2] == "Changed manager settings: launch.port 7777 → 7790, rcon.enabled off → on, rcon.password changed (by testclient)"
    assert lines[3].startswith("Changed server settings: ") and 'server_name' in lines[3] and '"Audited"' in lines[3] and "password changed" in lines[3]
    assert lines[4:] == [
        "Banned 76561198000000001 (by testclient)",
        "Saved profile weekend (by testclient)",
        f"Created backup {name} (by testclient)",
        f"Deleted backup {name} (by testclient)",
        "Signed out (by testclient)",
    ]
    # On disk: in its own file and in the main log, and never with a secret in it.
    audit = (m.paths.logs / "audit.log").read_text(encoding="utf-8")
    assert audit.count("\n") == len(lines) and "Signed in with the admin password (by testclient)" in audit
    everything = audit + (m.paths.logs / "manager.log").read_text(encoding="utf-8")
    assert "Changed manager settings" in everything
    for secret in ("rcon-secret-1", "joinsecret", PASSWORD, "wrong-password"):
        assert secret not in everything


def test_password_change_and_refusal_are_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    with TestClient(create_app()) as client:
        m = client.app.state.manager
        generated = m.auth.bootstrap() or ""
        m.auth.change_password("", "first-password-1", first_time=True) if not generated else None
        current = generated or "first-password-1"
        client.post("/api/auth/login", json={"password": current})
        assert client.post("/api/auth/password", json={"current": "nope-nope", "new": "second-password-2"}).status_code == 401
        assert client.post("/api/auth/password", json={"current": current, "new": "second-password-2"}).status_code == 200
        lines = audit_lines(m)
        assert lines[-2] == "Admin password change refused: Current password is incorrect. (by testclient)"
        assert lines[-1].startswith("Changed the admin password")
        assert "second-password-2" not in (m.paths.logs / "manager.log").read_text(encoding="utf-8")


def test_who_is_named(setup):
    client, m = setup
    client.post("/api/auth/login", json={"password": PASSWORD})
    client.post("/api/server/start")
    assert audit_lines(m)[-1] == "Asked the game server to start (by testclient)"
