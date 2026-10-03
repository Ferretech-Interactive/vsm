import hashlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from vsm import __version__
from vsm.app import create_app
from vsm.core import selfupdate
from vsm.core.selfupdate import assets, choose, expected_hash

DOWNLOAD = "https://github.com/Ferretech-Interactive/vsm/releases/download/v9.0.0/"
WHEEL = "vein_server_manager-9.0.0-py3-none-any.whl"


def release(*names, base=DOWNLOAD):
    return {"tag_name": "v9.0.0", "html_url": "https://github.com/Ferretech-Interactive/vsm/releases/tag/v9.0.0", "assets": [{"name": n, "browser_download_url": base + n} for n in names]}


def test_release_needs_a_wheel_and_checksums_from_this_repository():
    assert assets(release(WHEEL, "SHA256SUMS.txt", "VSM 9.0.0 Setup.exe")) == (WHEEL, DOWNLOAD + WHEEL, DOWNLOAD + "SHA256SUMS.txt")
    assert assets(release(WHEEL)) is None  # nothing to check the download against
    assert assets(release("SHA256SUMS.txt", "VSM 9.0.0 Setup.exe")) is None  # a Windows-only release
    assert assets(release("something_else-9.0.0-py3-none-any.whl", "SHA256SUMS.txt")) is None
    assert assets(release(WHEEL, "SHA256SUMS.txt", base="https://github.com/someone-else/vsm/releases/download/v9.0.0/")) is None
    assert assets(None) is None


def test_checksum_lookup():
    sums = f"{'a' * 64}  VSM 9.0.0 Setup.exe\n{'B' * 64}  {WHEEL}\nnot a line\n"
    assert expected_hash(sums, WHEEL) == "b" * 64
    assert expected_hash(sums, "VSM 9.0.0 Setup.exe") is None  # names with spaces are not ours to install
    assert expected_hash(sums, "missing.whl") is None


def entry(version, folder="/data/manager", **more):
    return {"version": version, "python": str(Path(folder) / "versions" / version / "bin" / "python"), "tried": False, "healthy": False, **more}


def test_start_hands_over_to_a_newer_version_once():
    chosen, state = choose({"current": entry("9.0.0")}, "1.0.0", exists=lambda p: True)
    assert chosen["version"] == "9.0.0" and state["current"]["tried"] is True
    # It never reported healthy, so the next start runs the installed program and remembers why.
    chosen, state = choose(state, "1.0.0", exists=lambda p: True)
    assert chosen is None and state["current"] is None and state["failed"]["version"] == "9.0.0"


def test_a_failed_version_falls_back_to_the_one_before_it():
    state = {"current": entry("9.0.0", tried=True), "previous": entry("8.0.0", tried=True, healthy=True)}
    chosen, state = choose(state, "1.0.0", exists=lambda p: True)
    assert chosen["version"] == "8.0.0" and state["current"]["version"] == "8.0.0" and state["previous"] is None
    assert state["failed"]["version"] == "9.0.0"


def test_a_healthy_version_keeps_being_used():
    state = {"current": entry("9.0.0", tried=True, healthy=True)}
    for _ in range(3):
        chosen, state = choose(state, "1.0.0", exists=lambda p: True)
        assert chosen["version"] == "9.0.0"


@pytest.mark.parametrize("own, exists", [("9.0.0", True), ("9.5.0", True), ("1.0.0", False)])
def test_ignored_when_the_installed_program_caught_up_or_the_files_are_gone(own, exists):
    chosen, state = choose({"current": entry("9.0.0", tried=True, healthy=True)}, own, exists=lambda p: exists)
    assert chosen is None and state["current"] is None and state["failed"] is None


def test_garbage_state_is_harmless():
    assert choose({"current": "nonsense", "previous": {"version": "x"}}, "1.0.0")[0] is None
    assert choose({}, "1.0.0") == (None, {"current": None, "previous": None, "failed": None})


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    with TestClient(create_app()) as client:
        yield client, client.app.state.manager


def test_not_offered_where_the_manager_cannot_restart_itself(setup):
    client, m = setup
    client.post("/api/auth/login", json={"password": "test-password"})
    assert client.get("/api/status").json()["manager_update"]["supported"] is False
    assert client.get("/api/update").json()["self_update"] is False
    assert client.post("/api/update/self").status_code == 409


def test_needs_signing_in(setup):
    client, _ = setup
    assert client.post("/api/update/self").status_code == 401


def test_update_installs_beside_the_running_version_and_restarts(setup, monkeypatch):
    client, m = setup
    client.post("/api/auth/login", json={"password": "test-password"})
    wheel = b"wheel bytes"
    files = {DOWNLOAD + WHEEL: wheel, DOWNLOAD + "SHA256SUMS.txt": f"{hashlib.sha256(wheel).hexdigest()}  {WHEEL}\n".encode()}
    restarted, commands = [], []

    def call(self, command):
        commands.append(command)
        if command[1:3] == ["-m", "venv"]:
            (m.self_update.folder / "versions" / "9.0.0" / "bin").mkdir(parents=True)
        return "9.0.0\n"

    monkeypatch.setattr(selfupdate, "SUPPORTED", True)
    monkeypatch.setattr(selfupdate, "_fetch", lambda url, limit: files[url])
    monkeypatch.setattr(selfupdate.SelfUpdate, "_call", call)
    monkeypatch.setattr(m.updates, "_fetch", lambda: [release(WHEEL, "SHA256SUMS.txt")])
    m.restart_hook = lambda: restarted.append(m.reexec)

    assert client.get("/api/update").json()["self_update"] is True
    assert client.post("/api/update/self").json() == {"ok": True, "version": "v9.0.0"}
    for t in __import__("threading").enumerate():
        if t.name == "self-update":
            t.join(10)

    python = str(m.self_update.folder / "versions" / "9.0.0" / "bin" / "python")
    assert restarted == [python]
    assert commands[1][:4] == [python, "-m", "pip", "install"] and commands[1][-1].endswith(WHEEL + "[discord]")
    state = selfupdate.read_state(m.self_update.folder)
    assert state["current"] == {"version": "9.0.0", "python": python, "tried": True, "healthy": False} and state["previous"] is None
    assert not (m.self_update.folder / WHEEL).exists()  # the download is not kept
    assert m.self_update.status()["state"] == "restarting"
    events = [e["message"] for e in client.get("/api/logs").json()["events"]]
    assert any("Started updating the manager to v9.0.0" in e for e in events)  # audited


def test_a_download_that_fails_its_checksum_is_not_installed(setup, monkeypatch):
    client, m = setup
    client.post("/api/auth/login", json={"password": "test-password"})
    files = {DOWNLOAD + WHEEL: b"tampered", DOWNLOAD + "SHA256SUMS.txt": f"{'0' * 64}  {WHEEL}\n".encode()}
    monkeypatch.setattr(selfupdate, "SUPPORTED", True)
    monkeypatch.setattr(selfupdate, "_fetch", lambda url, limit: files[url])
    monkeypatch.setattr(selfupdate.SelfUpdate, "_call", lambda self, command: pytest.fail("nothing may be run"))
    monkeypatch.setattr(m.updates, "_fetch", lambda: [release(WHEEL, "SHA256SUMS.txt")])
    m.restart_hook = lambda: pytest.fail("must not restart")
    client.post("/api/update/self")
    for t in __import__("threading").enumerate():
        if t.name == "self-update":
            t.join(10)
    status = m.self_update.status()
    assert status["state"] == "failed" and "checksum" in status["error"]
    assert selfupdate.read_state(m.self_update.folder) == {}


def test_settle_trusts_the_running_version_and_removes_old_ones(setup):
    client, m = setup
    versions = m.self_update.folder / "versions"
    for name in (__version__, "0.0.1", "0.0.2"):
        (versions / name).mkdir(parents=True)
    selfupdate.write_state(m.self_update.folder, {"current": entry(__version__, m.self_update.folder, tried=True), "previous": entry("0.0.2", m.self_update.folder, healthy=True), "failed": {"version": "0.0.9", "at": 1}})
    m.self_update.settle()
    assert selfupdate.read_state(m.self_update.folder)["current"]["healthy"] is True
    assert sorted(p.name for p in versions.iterdir()) == sorted([__version__, "0.0.2"])
    assert m.self_update.status()["rolled_back"] == "0.0.9"


def test_sign_in_survives_the_in_place_restart(setup, monkeypatch, tmp_path):
    client, m = setup
    client.post("/api/auth/login", json={"password": "test-password"})
    selfupdate._keep_sessions(m)
    kept = (m.self_update.folder / "sessions.json").read_text()
    assert client.cookies.get("vsm_session") not in kept  # hashes only, never the cookie itself
    m.auth._sessions.clear()  # what a new process starts with
    assert client.get("/api/config").status_code == 401
    selfupdate._restore_sessions(m)
    assert client.get("/api/config").status_code == 200
    assert not (m.self_update.folder / "sessions.json").exists()  # used once


def test_stale_or_forged_session_files_are_ignored(setup):
    import os
    import time

    client, m = setup
    m.self_update.folder.mkdir(parents=True, exist_ok=True)
    path = m.self_update.folder / "sessions.json"
    path.write_text('{"sessions": {"%s": %d}, "local": []}' % ("a" * 64, time.time() + 3600))
    old = time.time() - 600
    os.utime(path, (old, old))
    selfupdate._restore_sessions(m)
    assert m.auth._sessions == {} and not path.exists()
    m.auth.import_sessions({"sessions": {"short": time.time() + 60, "b" * 64: time.time() + 10**9, "c" * 64: "soon"}, "local": ["b" * 64]})
    assert m.auth._sessions == {} and m.auth._local_sessions == set()
