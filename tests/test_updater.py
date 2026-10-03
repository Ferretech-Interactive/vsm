import pytest
from fastapi.testclient import TestClient

from vsm.desktop import updater
from vsm.desktop.updater import PUBLISHER, UpdateError, common_name, download, is_trusted, pick_release

REPO = "https://github.com/Ferretech-Interactive/vsm"


def release(tag, prerelease=False, assets=None, draft=False, page=None):
    version = tag.lstrip("v")
    if assets is None:
        assets = [
            {"name": f"VSM {version} Portable.zip", "browser_download_url": f"{REPO}/releases/download/{tag}/VSM.{version}.Portable.zip", "size": 10},
            {"name": f"VSM {version} Setup.exe", "browser_download_url": f"{REPO}/releases/download/{tag}/VSM.{version}.Setup.exe", "size": 20},
        ]
    return {"tag_name": tag, "prerelease": prerelease, "draft": draft, "html_url": page or f"{REPO}/releases/tag/{tag}", "assets": assets}


def test_picks_the_newest_stable_release_with_a_setup_file():
    releases = [release("v0.1.0"), release("v0.3.0-beta.1", prerelease=True), release("v0.2.0"), release("v0.2.1", draft=True), "junk", {}]
    stable, beta = pick_release(releases, "0.1.0b1")
    assert stable["tag"] == "v0.2.0" and stable["name"] == "VSM 0.2.0 Setup.exe" and stable["size"] == 20
    assert stable["url"] == f"{REPO}/releases/download/v0.2.0/VSM.0.2.0.Setup.exe"
    assert beta == {"tag": "v0.3.0-beta.1", "page": f"{REPO}/releases/tag/v0.3.0-beta.1"}  # pointed to, never installed


def test_nothing_newer_and_betas_older_than_stable():
    assert pick_release([release("v0.1.0"), release("v0.1.0-beta.2", prerelease=True)], "0.1.0") == (None, None)
    stable, beta = pick_release([release("v0.2.0"), release("v0.2.0-beta.3", prerelease=True)], "0.1.0")
    assert stable["tag"] == "v0.2.0" and beta is None
    assert pick_release([release("v0.2.0")], "not-a-version") == (None, None)


@pytest.mark.parametrize("assets", [
    [],  # no installer attached
    [{"name": "VSM 0.2.0 Setup.exe", "browser_download_url": "https://evil.example/VSM.0.2.0.Setup.exe", "size": 1}],  # hosted elsewhere
    [{"name": "VSM 0.2.0 Setup.exe", "browser_download_url": "https://github.com/someone-else/fork/releases/download/v0.2.0/x.exe", "size": 1}],
    [{"name": "totally-legit.exe", "browser_download_url": f"{REPO}/releases/download/v0.2.0/totally-legit.exe", "size": 1}],
])
def test_a_release_without_this_projects_setup_file_is_not_offered(assets):
    assert pick_release([release("v0.2.0", assets=assets)], "0.1.0") == (None, None)
    assert pick_release([release("v0.2.0", page="https://evil.example/releases/tag/v0.2.0")], "0.1.0") == (None, None)


def test_only_a_valid_signature_from_the_publisher_is_trusted():
    ours = f'CN="{PUBLISHER}", O="{PUBLISHER}", L=Somewhere, S=NY, C=US'
    assert common_name(ours) == PUBLISHER and common_name(f"CN={PUBLISHER}, O=x") == PUBLISHER
    assert is_trusted("Valid", ours) is True
    assert is_trusted("NotSigned", "") is False  # a beta, or anything unsigned
    assert is_trusted("HashMismatch", ours) is False  # tampered with after signing
    assert is_trusted("UnknownError", ours) is False  # chain not trusted
    assert is_trusted("Valid", "CN=Someone Else LLC, O=Someone Else LLC, C=US") is False
    assert is_trusted("Valid", f"CN=Evil, O={PUBLISHER}, OU=CN={PUBLISHER}") is False  # the name must be the CN itself
    assert is_trusted("Valid", f"CN={PUBLISHER} Fake, C=US") is False


def test_download_refuses_what_is_not_ours(tmp_path, monkeypatch):
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: pytest.fail("nothing may be fetched"))
    good = {"name": "VSM 0.2.0 Setup.exe", "url": f"{REPO}/releases/download/v0.2.0/VSM.0.2.0.Setup.exe", "size": 20}
    for bad in ({**good, "url": "https://evil.example/x.exe"}, {**good, "name": "..\\evil.exe"}, {**good, "size": updater.MAX_SETUP_BYTES + 1}):
        with pytest.raises(UpdateError):
            download(bad, tmp_path)


def test_download_checks_the_size_and_cleans_up(tmp_path, monkeypatch):
    import io

    good = {"name": "VSM 0.2.0 Setup.exe", "url": f"{REPO}/releases/download/v0.2.0/VSM.0.2.0.Setup.exe", "size": 5}
    (tmp_path / "VSM 0.1.9 Setup.exe").write_bytes(b"old")
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"12345"))
    path = download(good, tmp_path)
    assert path.read_bytes() == b"12345" and [p.name for p in tmp_path.iterdir()] == ["VSM 0.2.0 Setup.exe"]  # the old download is gone
    monkeypatch.setattr(updater.urllib.request, "urlopen", lambda *a, **k: io.BytesIO(b"123"))
    with pytest.raises(UpdateError):
        download(good, tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing half-downloaded is left to run


def test_only_the_desktop_window_on_an_installed_host_may_ask_for_an_install(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    from vsm.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as c:
        c.post("/api/auth/login", json={"password": "test-password"})
        m = c.app.state.manager
        monkeypatch.setattr(m.updates, "check", lambda: {"current": "0.1.0", "available": True, "latest": "v0.2.0", "url": "u", "prerelease": False})
        # A password session (a browser, or another machine) never gets one click, even on an installed host.
        m.service_mode = True
        assert c.get("/api/update").json()["one_click"] is False
        assert c.post("/api/update/install").status_code == 403 and c.get("/api/update/install-request").status_code == 403
        assert m.update_requested_at == 0

        # The desktop window's own session does, for a stable release only.
        m.auth.enable_local_codes()
        assert c.post("/api/auth/local", json={"token": m.auth.issue_local_code()}).status_code == 200
        assert c.get("/api/update").json()["one_click"] is True
        assert c.get("/api/update/install-request").json() == {"requested": False}
        assert c.post("/api/update/install").status_code == 200
        assert c.get("/api/update/install-request").json() == {"requested": True}
        assert c.get("/api/update/install-request").json() == {"requested": False}  # handed out once
        monkeypatch.setattr(m.updates, "check", lambda: {"current": "0.1.0", "available": True, "latest": "v0.2.0-beta.1", "url": "u", "prerelease": True})
        assert c.get("/api/update").json()["one_click"] is False
        m.service_mode = False  # the portable build is replaced by hand
        assert c.post("/api/update/install").status_code == 403
