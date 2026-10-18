"""A server stopped by hand stays stopped when the manager next starts, even with auto-start on."""
import json

import pytest
from fastapi.testclient import TestClient

from vsm.app import create_app


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    with TestClient(create_app()) as client:
        manager = client.app.state.manager
        manager.paths.server_exe.parent.mkdir(parents=True, exist_ok=True)
        manager.paths.server_exe.write_bytes(b"exe")
        manager.config.update({"behavior": {"auto_start": True}})
        yield manager


def boot(m, monkeypatch):
    started = []
    monkeypatch.setattr(m.server, "_find_pid", lambda: None)
    monkeypatch.setattr(m.server, "request_start", lambda trigger="USER": started.append(trigger))
    m.server._boot()
    return started


def test_nothing_recorded_means_auto_start_as_before(m, monkeypatch):
    assert m.server.wanted() is True
    assert boot(m, monkeypatch) == ["AUTOSTART"]


def test_stopped_by_hand_stays_stopped_until_started_by_hand(m, monkeypatch):
    calls = []
    monkeypatch.setattr(m.server, "_terminate", lambda pid: calls.append(pid))
    m.server._do_stop("USER")
    assert m.server.wanted() is False
    said = []
    monkeypatch.setattr(m, "event", lambda category, message: said.append(message))
    assert boot(m, monkeypatch) == []
    assert any("stopped by hand" in message for message in said)
    # Starting it by hand puts it back to normal.
    m.server._note_wanted(True, "USER")
    assert boot(m, monkeypatch) == ["AUTOSTART"]


@pytest.mark.parametrize("trigger", ["SHUTDOWN", "SCHEDULER", "WATCHDOG", "AUTO_UPDATE", "RAM_LIMIT", "AUTOSTART"])
def test_stops_nobody_asked_for_do_not_count(m, trigger):
    m.server._note_wanted(False, trigger)
    assert m.server.wanted() is True


def test_discord_and_setup_count_as_by_hand(m):
    m.server._note_wanted(False, "DISCORD")
    assert m.server.wanted() is False
    m.server._note_wanted(True, "SETUP")
    assert m.server.wanted() is True


def test_a_restart_by_hand_is_not_a_stop(m, monkeypatch):
    monkeypatch.setattr(m.server, "_terminate", lambda pid: None)
    monkeypatch.setattr("vsm.server.controller.time.sleep", lambda s: None)
    monkeypatch.setattr(m.server, "_do_start", lambda trigger: m.server._note_wanted(True, trigger))
    m.server._note_wanted(False, "USER")
    m.server._do_restart("USER")
    assert m.server.wanted() is True


def test_a_damaged_record_does_not_block_starting(m, monkeypatch):
    (m.paths.data / "server-state.json").write_text("not json")
    assert m.server.wanted() is True
    (m.paths.data / "server-state.json").write_text(json.dumps(["odd"]))
    assert m.server.wanted() is True
