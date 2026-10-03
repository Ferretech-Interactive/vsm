import time

import pytest
from fastapi.testclient import TestClient

from vsm.app import create_app
from vsm.server import branches
from vsm.server.branches import parse_branches, parse_vdf

OUTPUT = r'''Redirecting stderr to 'C:\steamcmd\logs\stderr.txt'
Loading Steam API..."OK"
AppID : 2131400, change number : 31000000/0, last change : Sat Oct  3 20:00:00 2026
"2131400"
{
	"common"
	{
		"name"		"VEIN Dedicated Server"
	}
	"depots"
	{
		"2131401"
		{
			"manifests"
			{
				"public"
				{
					"gid"		"123"
				}
			}
		}
		"branches"
		{
			"public"
			{
				"buildid"		"25708939"
				"timeupdated"		"1791000000"
			}
			"experimental"
			{
				"buildid"		"25710126"
				"description"		"Bleeding-edge \"updates\""
				"timeupdated"		"1791003000"
			}
			"internal"
			{
				"buildid"		"25000000"
				"pwdrequired"		"1"
				"timeupdated"		"1790000000"
			}
			"bad name!"
			{
				"buildid"		"1"
			}
		}
	}
}
Unloading Steam API..."OK"
'''


def test_parse_branches():
    found = parse_branches(OUTPUT)
    assert [b["name"] for b in found] == ["experimental", "public", "internal"]  # newest build first
    assert found[0] == {"name": "experimental", "description": 'Bleeding-edge "updates"', "build_id": 25710126, "updated": 1791003000, "password_required": False}
    assert found[1]["description"] == "" and found[2]["password_required"] is True


@pytest.mark.parametrize("text", ["", "No app info", '"2131400"\n{\n"depots"\n{\n', '"2131400"\n{ "depots" { "branches" "oops" } }', '"2131400"\n}'])
def test_parse_branches_rejects_garbage(text):
    assert parse_branches(text) == []


def test_parse_vdf_depth_limit():
    with pytest.raises(ValueError):
        parse_vdf('"a" {' * 40)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    with TestClient(create_app()) as client:
        client.post("/api/auth/login", json={"password": "test-password"})
        m = client.app.state.manager
        monkeypatch.setattr(m.steam, "ensure", lambda: None)
        yield client, m


def wait(client):
    deadline = time.time() + 5
    while time.time() < deadline:
        state = client.get("/api/steam/branches").json()
        if not state["loading"]:
            return state
        time.sleep(0.02)
    raise AssertionError("still loading")


def test_refresh_runs_in_the_background_and_is_stored(setup, monkeypatch):
    client, m = setup
    release = __import__("threading").Event()

    def run(cmd):
        assert "+app_info_print" in cmd
        release.wait(5)
        return OUTPUT

    monkeypatch.setattr(m.branches, "_run", run)
    first = client.get("/api/steam/branches").json()  # nothing stored: starts a refresh, answers at once
    assert first["loading"] is True and first["branches"] == []
    assert m.branches.refresh() is False  # only one at a time
    release.set()
    state = wait(client)
    assert [b["name"] for b in state["branches"]] == ["experimental", "public", "internal"]
    assert state["error"] == "" and state["checked_at"] > 0
    assert branches.Branches(m).status()["branches"] == state["branches"]  # read back from disk

    monkeypatch.setattr(m.branches, "_run", lambda cmd: pytest.fail("a fresh list must not be fetched again"))
    assert client.get("/api/steam/branches").json()["loading"] is False


def test_failure_keeps_the_old_list(setup, monkeypatch):
    client, m = setup
    monkeypatch.setattr(m.branches, "_run", lambda cmd: OUTPUT)
    client.post("/api/steam/branches/refresh")
    assert len(wait(client)["branches"]) == 3

    monkeypatch.setattr(m.branches, "_run", lambda cmd: "Loading Steam API...OK\n")
    client.post("/api/steam/branches/refresh")
    state = wait(client)
    assert "did not return" in state["error"] and len(state["branches"]) == 3


def test_cancelled_refresh_does_not_run_steamcmd_again(setup, monkeypatch):
    client, m = setup
    runs = []

    def run(cmd):
        runs.append(1)
        m.branches.cancel()  # an install started: SteamCMD was killed and printed nothing useful
        return ""

    monkeypatch.setattr(m.branches, "_run", run)
    client.post("/api/steam/branches/refresh")
    assert "install or update" in wait(client)["error"] and len(runs) == 1
    assert m.steam.lock.acquire(blocking=False)  # released straight away for the install
    m.steam.lock.release()


def test_busy_steamcmd_is_reported(setup, monkeypatch):
    client, m = setup
    monkeypatch.setattr(m.branches, "_run", lambda cmd: pytest.fail("must not run beside an update"))
    with m.steam.lock:
        client.post("/api/steam/branches/refresh")
        assert "busy" in wait(client)["error"]
