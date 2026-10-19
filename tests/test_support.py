"""The diagnostics zip behind "Report a problem": what goes in, and what never does."""
import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient

from vsm.core.support import Scrubber

PASSWORD = "correct-horse-battery"
WEBHOOK = "https://discord.com/api/webhooks/123456789012345678/abcdefghijklmnopqrstuvwxyz0123456789ABCDEFGHIJ"
BOT_TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GabCdE.abcdefghijklmnopqrstuvwxyz0123456"
STEAM_ID = "76561198012345678"


@pytest.fixture
def authed(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
        yield c


def unzip(response) -> dict[str, str]:
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "vsm-diagnostics-" in response.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def test_secrets_are_always_removed():
    s = Scrubber(["hunter2-server"], anonymize=False)
    text = s.text(
        f"webhook {WEBHOOK}\nbot {BOT_TOKEN}\nAuthorization: Bearer abc.def\nPassword=hunter2-server\n"
        "?Name=Bob?Password=letmein?ID=1\n\"api_key\": \"xyz123\"\nechoed hunter2-server again\n"
    )
    for secret in (WEBHOOK, BOT_TOKEN, "abc.def", "hunter2-server", "letmein", "xyz123"):
        assert secret not in text
    assert "?Name=Bob" in text  # nothing else of the line is lost
    assert "password_required" in s.text("password_required")  # a key with no value stays


def test_anonymize_replaces_who_and_where_consistently():
    s = Scrubber([], anonymize=True, names=["Wanderer"])
    text = s.text(
        f"Heartbeating with IP 81.2.69.142:7777 from 192.168.1.50\nJoin {STEAM_ID} Name=Wanderer\n"
        f"left {STEAM_ID} Wanderer\nC:\\Users\\jdoe\\AppData /home/jdoe/vsm v1.2.3.4\n"
    )
    assert "81.2.69.142" not in text and "<ip-1>:7777" in text
    assert "192.168.1.50" in text  # private addresses help and say nothing about where it is
    assert STEAM_ID not in text and text.count("<steamid-1>") == 2
    assert "Wanderer" not in text and text.count("<player-1>") == 2
    assert "jdoe" not in text and "C:\\Users\\<user>\\AppData" in text and "/home/<user>/vsm" in text


def test_not_anonymized_keeps_who_and_where():
    s = Scrubber([], anonymize=False, names=["Wanderer"])
    assert s.text(f"81.2.69.142 {STEAM_ID} Wanderer") == f"81.2.69.142 {STEAM_ID} Wanderer"


def test_bundle_contents_and_redaction(authed, monkeypatch):
    monkeypatch.setattr("vsm.api.routes.public_ip", lambda: "81.2.69.142")  # the health check asks the internet otherwise
    m = authed.app.state.manager
    m.config.update({"discord": {"webhook_url": WEBHOOK, "bot_token": BOT_TOKEN}})
    m.paths.game_ini.parent.mkdir(parents=True, exist_ok=True)
    m.paths.game_ini.write_text("[/Script/Vein.VeinGameSession]\nServerName=Test\nPassword=sekrit-pass\nSuperAdminSteamIDs=" + STEAM_ID + "\n")
    m.paths.game_log.parent.mkdir(parents=True, exist_ok=True)
    m.paths.game_log.write_text(f"LogNet: Join request: ?Name=Wanderer?ID={STEAM_ID}\nHeartbeating with IP 81.2.69.142:7777\n")
    m.history.seen(STEAM_ID, "Wanderer")
    m.event("SYSTEM", "Something happened")

    info = authed.get("/api/support").json()
    assert {"summary.json", "config.json", "events.txt", "health-check.json", "game/Vein.log", "game/Game.ini", "logs/manager.log"} <= set(info["files"])
    assert info["desktop_log"] is False and info["version"] and info["install"] and info["os"]

    files = unzip(authed.get("/api/support/bundle"))
    assert set(info["files"]) <= set(files) and "README.txt" in files
    everything = "\n".join(files.values())
    for secret in (WEBHOOK, BOT_TOKEN, "sekrit-pass", PASSWORD, STEAM_ID, "81.2.69.142", "Wanderer"):
        assert secret not in everything, secret
    assert not any(n.endswith(("auth.json", "sessions.json")) for n in files)
    assert json.loads(files["summary.json"])["version"] == info["version"]
    assert "Something happened" in files["events.txt"]
    assert "ServerName=Test" in files["game/Game.ini"]

    plain = "\n".join(unzip(authed.get("/api/support/bundle?anonymize=false")).values())
    assert STEAM_ID in plain and "Wanderer" in plain
    assert "sekrit-pass" not in plain and BOT_TOKEN not in plain  # secrets go either way
    assert any("Downloaded diagnostics" in e["message"] for e in m.hub.snapshot("event"))


def test_desktop_log_only_from_the_app_on_this_pc(authed):
    # A password sign-in is not the desktop window's local session.
    assert authed.post("/api/support/desktop-log", json={"text": "hello"}).status_code == 403


def test_desktop_log_is_included_when_handed_over(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("VSM_LOCAL_LOGIN", "1")
    from vsm.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000)) as c:
        token = (tmp_path / "desktop-auth" / "desktop.token").read_text()
        assert c.post("/api/auth/local", json={"token": token}).status_code == 200
        monkeypatch.setattr("vsm.api.routes.public_ip", lambda: "unknown")
        assert c.post("/api/support/desktop-log", json={"text": f"window opened, webhook {WEBHOOK}"}).status_code == 200
        assert c.get("/api/support").json()["desktop_log"] is True
        files = unzip(c.get("/api/support/bundle"))
        assert "window opened" in files["app/desktop.log"] and WEBHOOK not in files["app/desktop.log"]


def test_requires_sign_in(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        assert c.get("/api/support").status_code == 401
        assert c.get("/api/support/bundle").status_code == 401


def test_portable_desktop_log_is_read_from_the_shared_logs_folder(authed, monkeypatch):
    monkeypatch.setattr("vsm.api.routes.public_ip", lambda: "unknown")
    m = authed.app.state.manager
    (m.paths.logs / "desktop.log").write_text(f"window opened, webhook {WEBHOOK}\n")
    assert "app/desktop.log" in authed.get("/api/support").json()["files"]
    files = unzip(authed.get("/api/support/bundle"))
    assert "window opened" in files["app/desktop.log"] and WEBHOOK not in files["app/desktop.log"]


def test_job_reports_progress_and_hands_the_zip_over_once(authed, monkeypatch):
    import time

    monkeypatch.setattr("vsm.api.routes.public_ip", lambda: "unknown")
    job = authed.post("/api/support/jobs", json={"anonymize": True}).json()["id"]
    for _ in range(100):
        status = authed.get(f"/api/support/jobs/{job}").json()
        assert status["state"] in ("working", "done") and 0 <= status["done"] <= status["total"]
        if status["state"] == "done":
            break
        time.sleep(0.05)
    assert status["state"] == "done" and status["done"] == status["total"]
    files = unzip(authed.get(f"/api/support/jobs/{job}/zip"))
    assert "summary.json" in files and "health-check.json" in files
    assert authed.get(f"/api/support/jobs/{job}/zip").status_code == 404  # handed over once, then dropped
    assert authed.get("/api/support/jobs/not-a-job").status_code == 404


def test_a_second_start_while_working_joins_the_first(authed):
    m = authed.app.state.manager
    with m.support.lock:
        m.support.id, m.support.state = "busy-job", "working"
    assert authed.post("/api/support/jobs", json={"anonymize": True}).json()["id"] == "busy-job"
