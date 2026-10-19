import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"


def make_client(tmp_path, monkeypatch, *, local_login: bool, peer: str = "127.0.0.1", env_password: bool = False):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    if env_password:
        monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    if local_login:
        monkeypatch.setenv("VSM_LOCAL_LOGIN", "1")
    else:
        monkeypatch.delenv("VSM_LOCAL_LOGIN", raising=False)
    from vsm.app import create_app

    return TestClient(create_app(), client=(peer, 50000))


def token(tmp_path) -> str:
    return (tmp_path / "desktop-auth" / "desktop.token").read_text()


def test_disabled_by_default(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=False) as c:
        assert not (tmp_path / "desktop-auth" / "desktop.token").exists()
        assert c.post("/api/auth/local", json={"token": "anything"}).status_code == 403


def test_loopback_token_signs_in(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=True) as c:
        assert c.get("/api/status").status_code == 401
        assert c.post("/api/auth/local", json={"token": "wrong"}).status_code == 401
        assert c.post("/api/auth/local", json={"token": token(tmp_path)}).status_code == 200
        state = c.get("/api/auth/state").json()
        assert state == {"authenticated": True, "version": state["version"], "local": True, "needs_password": True, "service_mode": False}
        assert c.get("/api/status").status_code == 200


def test_remote_peer_refused(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=True, peer="192.168.1.50") as c:
        assert c.post("/api/auth/local", json={"token": token(tmp_path)}).status_code == 403


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "X-Real-IP"])
def test_proxied_request_refused(tmp_path, monkeypatch, header):
    # A reverse proxy on the same host connects from loopback on behalf of remote users.
    with make_client(tmp_path, monkeypatch, local_login=True) as c:
        r = c.post("/api/auth/local", json={"token": token(tmp_path)}, headers={header: "203.0.113.9"})
        assert r.status_code == 403


def test_wrong_tokens_do_not_lock_out_password_sign_in(tmp_path, monkeypatch):
    # Otherwise any local program could keep the administrator out by posting junk.
    with make_client(tmp_path, monkeypatch, local_login=True, env_password=True) as c:
        for _ in range(10):
            assert c.post("/api/auth/local", json={"token": "guess"}).status_code == 401
        assert c.post("/api/auth/local", json={"token": token(tmp_path)}).status_code == 200
        c.cookies.clear()
        assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def code_client(tmp_path, monkeypatch, peer="127.0.0.1"):
    """A manager set up the way the Windows service does it: one-time codes, no token file."""
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.delenv("VSM_LOCAL_LOGIN", raising=False)
    from vsm.app import create_app

    client = TestClient(create_app(), client=(peer, 50000))
    client.app.state.manager.auth.enable_local_codes()
    return client


def test_one_time_code_signs_in_once(tmp_path, monkeypatch):
    with code_client(tmp_path, monkeypatch) as c:
        auth = c.app.state.manager.auth
        code = auth.issue_local_code()
        assert c.post("/api/auth/local", json={"token": "wrong"}).status_code == 401
        assert c.post("/api/auth/local", json={"token": code}).status_code == 200
        assert c.get("/api/auth/state").json()["local"] is True
        c.cookies.clear()
        assert c.post("/api/auth/local", json={"token": code}).status_code == 401  # already used


def test_one_time_code_expires_and_store_is_capped(tmp_path, monkeypatch):
    from vsm.api import auth as auth_module

    with code_client(tmp_path, monkeypatch) as c:
        auth = c.app.state.manager.auth
        clock = [1000.0]
        monkeypatch.setattr(auth_module.time, "time", lambda: clock[0])
        code = auth.issue_local_code()
        clock[0] += auth_module.LOCAL_CODE_TTL + 1
        assert c.post("/api/auth/local", json={"token": code}).status_code == 401

        codes = [auth.issue_local_code() for _ in range(auth_module.MAX_LOCAL_CODES + 5)]
        assert len(auth._local_codes) == auth_module.MAX_LOCAL_CODES
        assert c.post("/api/auth/local", json={"token": codes[-1]}).status_code == 200


def test_one_time_code_still_needs_loopback_and_no_proxy(tmp_path, monkeypatch):
    with code_client(tmp_path, monkeypatch, peer="192.168.1.50") as c:
        code = c.app.state.manager.auth.issue_local_code()
        assert c.post("/api/auth/local", json={"token": code}).status_code == 403
    with code_client(tmp_path, monkeypatch) as c:
        code = c.app.state.manager.auth.issue_local_code()
        assert c.post("/api/auth/local", json={"token": code}, headers={"X-Forwarded-For": "203.0.113.9"}).status_code == 403


def test_token_changes_every_start(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=True):
        first = token(tmp_path)
    with make_client(tmp_path, monkeypatch, local_login=True) as c:
        assert token(tmp_path) != first
        assert c.post("/api/auth/local", json={"token": first}).status_code == 401


def test_local_session_sets_password_without_current(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=True) as c:
        c.post("/api/auth/local", json={"token": token(tmp_path)})
        assert c.post("/api/auth/password", json={"new": "short"}).status_code == 400
        assert c.post("/api/auth/password", json={"new": PASSWORD}).status_code == 200
        # the desktop session survives; the password now works and the prompt is gone
        state = c.get("/api/auth/state").json()
        assert state["authenticated"] and not state["needs_password"]
        c.cookies.clear()
        assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_password_session_still_needs_current(tmp_path, monkeypatch):
    with make_client(tmp_path, monkeypatch, local_login=True) as c:
        c.post("/api/auth/local", json={"token": token(tmp_path)})
        c.post("/api/auth/password", json={"new": PASSWORD})
        c.cookies.clear()
        c.post("/api/auth/login", json={"password": PASSWORD})
        assert c.get("/api/auth/state").json()["local"] is False
        assert c.post("/api/auth/password", json={"new": "another-password"}).status_code == 401
        assert c.post("/api/auth/password", json={"current": PASSWORD, "new": "another-password"}).status_code == 200
