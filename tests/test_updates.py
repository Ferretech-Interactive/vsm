import pytest
from fastapi.testclient import TestClient

from vsm.core import updates
from vsm.core.updates import parse

PASSWORD = "correct-horse-battery"
REPO = "https://github.com/Ferretech-Interactive/vsm"


def release(tag, **extra):
    return {"tag_name": tag, "html_url": f"{REPO}/releases/tag/{tag}", "draft": False, **extra}


def test_tag_and_package_spellings_are_the_same_version():
    assert parse("v0.1.0-beta.2") == parse("0.1.0b2") == (0, 1, 0, 1, 2)
    assert parse("v1.2.3") == parse("1.2.3") == (1, 2, 3, 3, 0)
    assert parse("v0.1.0-rc.1") == parse("0.1.0rc1")
    assert parse("nightly") is None and parse("v1.2") is None


def test_ordering():
    order = ["0.0.1", "0.1.0-alpha.1", "0.1.0-beta.1", "0.1.0-beta.2", "0.1.0-rc.1", "0.1.0", "0.1.1-beta.1", "0.2.0", "1.0.0"]
    parsed = [parse(v) for v in order]
    assert parsed == sorted(parsed)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        yield c


def offer(client, monkeypatch, current, releases):
    monkeypatch.setattr(updates, "__version__", current)
    m = client.app.state.manager
    m.updates._cached = None
    monkeypatch.setattr(m.updates, "_fetch", lambda: releases)
    return client.get("/api/update").json()


def test_stable_user_is_offered_stable_only(client, monkeypatch):
    releases = [release("v0.3.0-beta.1"), release("v0.2.0"), release("v0.1.0"), release("v9.9.9", draft=True)]
    r = offer(client, monkeypatch, "0.1.0", releases)
    assert r == {"current": "0.1.0", "available": True, "latest": "v0.2.0", "url": f"{REPO}/releases/tag/v0.2.0", "prerelease": False, "one_click": False, "self_update": False}
    # opting in to betas changes the answer (and is not served from the cache)
    client.put("/api/config", json={"behavior": {"update_prereleases": True}})
    assert client.get("/api/update").json()["latest"] == "v0.3.0-beta.1"


def test_beta_user_keeps_getting_betas(client, monkeypatch):
    r = offer(client, monkeypatch, "0.1.0b1", [release("v0.1.0-beta.2"), release("v0.1.0-beta.1")])
    assert r["available"] and r["latest"] == "v0.1.0-beta.2" and r["prerelease"] is True
    # and the stable release of the same version counts as newer than its betas
    assert offer(client, monkeypatch, "0.1.0b2", [release("v0.1.0")])["latest"] == "v0.1.0"


def test_nothing_newer_offline_or_disabled(client, monkeypatch):
    assert offer(client, monkeypatch, "0.2.0", [release("v0.2.0"), release("v0.1.0")])["available"] is False

    def offline():
        raise OSError("no network")

    monkeypatch.setattr(client.app.state.manager.updates, "_fetch", offline)
    client.app.state.manager.updates._cached = None
    assert client.get("/api/update").json()["available"] is False

    client.put("/api/config", json={"behavior": {"update_check": False}})
    assert offer(client, monkeypatch, "0.1.0", [release("v0.2.0")])["available"] is False


def test_links_outside_the_project_are_ignored(client, monkeypatch):
    spoofed = {"tag_name": "v9.0.0", "html_url": "https://evil.example/download", "draft": False}
    assert offer(client, monkeypatch, "0.1.0", [spoofed])["available"] is False
