"""The first password an installer can leave beside auth.json (`install.sh --password-file`)."""
from fastapi.testclient import TestClient

from vsm.app import create_app


def start(tmp_path, monkeypatch, env_password=None):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    if env_password:
        monkeypatch.setenv("VSM_ADMIN_PASSWORD", env_password)
    else:
        monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    return TestClient(create_app())


def login(client, password):
    return client.post("/api/auth/login", json={"password": password}).status_code


def test_used_once_then_deleted_and_changeable(tmp_path, monkeypatch):
    (tmp_path / "initial-password").write_text("chosen-by-the-installer\n")
    with start(tmp_path, monkeypatch) as client:
        assert not (tmp_path / "initial-password").exists()
        assert login(client, "chosen-by-the-installer") == 200
        assert client.get("/api/auth/state").json()["needs_password"] is False  # somebody chose it
        # Unlike VSM_ADMIN_PASSWORD, it does not pin the password.
        assert client.post("/api/auth/password", json={"current": "chosen-by-the-installer", "new": "something-else-now"}).status_code == 200
    with start(tmp_path, monkeypatch) as client:
        assert login(client, "something-else-now") == 200


def test_ignored_and_removed_when_a_password_already_exists(tmp_path, monkeypatch):
    (tmp_path / "initial-password").write_text("first-password-here")
    with start(tmp_path, monkeypatch):
        pass
    (tmp_path / "initial-password").write_text("a-later-attempt-here")
    with start(tmp_path, monkeypatch) as client:
        assert not (tmp_path / "initial-password").exists()
        assert login(client, "a-later-attempt-here") == 401
        assert login(client, "first-password-here") == 200


def test_too_short_or_with_an_env_password_it_is_not_used(tmp_path, monkeypatch):
    (tmp_path / "initial-password").write_text("short")
    with start(tmp_path, monkeypatch) as client:
        assert login(client, "short") == 401 and not (tmp_path / "initial-password").exists()
    other = tmp_path / "other"
    other.mkdir()
    (other / "initial-password").write_text("from-the-file-123456")
    with start(other, monkeypatch, env_password="from-the-environment") as client:
        assert login(client, "from-the-file-123456") == 401 and login(client, "from-the-environment") == 200
        assert not (other / "initial-password").exists()
