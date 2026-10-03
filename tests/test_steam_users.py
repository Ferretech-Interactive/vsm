import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from vsm import VsmError
from vsm.app import create_app
from vsm.integrations import steam_users
from vsm.integrations.steam_users import parse_profile, parse_query

GABE = "76561197960287930"
PROFILE = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?><profile>
	<steamID64>{GABE}</steamID64>
	<steamID><![CDATA[Rabscuttle]]></steamID>
	<onlineState>offline</onlineState>
	<avatarIcon><![CDATA[https://avatars.fastly.steamstatic.com/abc.jpg]]></avatarIcon>
	<avatarMedium><![CDATA[https://avatars.fastly.steamstatic.com/abc_medium.jpg]]></avatarMedium>
	<customURL><![CDATA[gabelogannewell]]></customURL>
	<groups><group><steamID64>103582791429521412</steamID64><groupName><![CDATA[Valve]]></groupName></group></groups>
</profile>"""
MISSING = '<?xml version="1.0" encoding="UTF-8"?><response><error><![CDATA[The specified profile could not be found.]]></error></response>'


@pytest.mark.parametrize("text, expected", [
    (GABE, ("profiles", GABE)),
    (f"  https://steamcommunity.com/profiles/{GABE}/  ", ("profiles", GABE)),
    (f"steamcommunity.com/profiles/{GABE}", ("profiles", GABE)),
    ("https://steamcommunity.com/id/gabelogannewell/", ("id", "gabelogannewell")),
    ("http://www.steamcommunity.com/id/Some_Name-1?l=english", ("id", "Some_Name-1")),
    ("gabelogannewell", ("id", "gabelogannewell")),
    ("STEAM_0:0:11101", ("profiles", GABE)),
    ("STEAM_1:0:11101", ("profiles", GABE)),
    ("[U:1:22202]", ("profiles", GABE)),
])
def test_parse_query(text, expected):
    assert parse_query(text) == expected


@pytest.mark.parametrize("text", ["", "1234", "12345678901234567", "https://example.com/id/x", "https://steamcommunity.com/id/a/../b c", "name with spaces", "https://steamcommunity.com/profiles/123", "x" * 40])
def test_parse_query_refuses(text):
    with pytest.raises(VsmError):
        parse_query(text)


def test_parse_profile():
    assert parse_profile(PROFILE) == {"id": GABE, "name": "Rabscuttle", "avatar": "https://avatars.fastly.steamstatic.com/abc_medium.jpg"}
    assert parse_profile(MISSING) is None
    assert parse_profile("<html>rate limited</html>") is None
    # An avatar anywhere else is dropped, and a name cannot carry control characters or run on.
    odd = PROFILE.replace("https://avatars.fastly.steamstatic.com/abc_medium.jpg", "https://evil.example/x.jpg").replace("Rabscuttle", "A\x00\n" + "b" * 100)
    parsed = parse_profile(odd)
    assert parsed["avatar"] == "" and parsed["name"] == "A" + "b" * 63


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "test-password")
    monkeypatch.setattr(steam_users, "PAUSE", 0)
    with TestClient(create_app()) as client:
        client.post("/api/auth/login", json={"password": "test-password"})
        yield client, client.app.state.manager


def test_lookup(setup, monkeypatch):
    client, m = setup
    calls = []

    def fetch(kind, value):
        calls.append((kind, value))
        return parse_profile(PROFILE) if value in ("gabelogannewell", GABE) else None

    monkeypatch.setattr(steam_users, "fetch", fetch)
    found = client.get("/api/steam/users/lookup", params={"q": "https://steamcommunity.com/id/gabelogannewell"}).json()
    assert found["id"] == GABE and found["name"] == "Rabscuttle" and calls == [("id", "gabelogannewell")]
    assert client.get("/api/steam/users/lookup", params={"q": "nobody-here"}).status_code == 404
    assert client.get("/api/steam/users/lookup", params={"q": "not a link"}).status_code == 400
    # The name was remembered, so listing it needs no further request.
    monkeypatch.setattr(steam_users, "fetch", lambda *a: pytest.fail("already known"))
    assert client.post("/api/steam/users", json={"ids": [GABE]}).json() == {"users": {GABE: {"name": "Rabscuttle", "avatar": "https://avatars.fastly.steamstatic.com/abc_medium.jpg"}}, "loading": False}
    assert steam_users.SteamUsers(m)._users[GABE]["name"] == "Rabscuttle"  # and stored on disk


def test_lookup_when_steam_is_unreachable(setup, monkeypatch):
    client, m = setup

    def fetch(kind, value):
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(steam_users, "fetch", fetch)
    m.history.seen(GABE, "InGameName")
    # A typed ID is still usable, with the name this server saw; a custom name cannot be resolved.
    assert client.get("/api/steam/users/lookup", params={"q": GABE}).json() == {"id": GABE, "name": "InGameName", "avatar": ""}
    assert client.get("/api/steam/users/lookup", params={"q": "somebody"}).status_code == 502


def test_names_are_filled_in_the_background(setup, monkeypatch):
    client, m = setup
    other = "76561197960287931"
    release = __import__("threading").Event()
    asked = []

    def fetch(kind, value):
        release.wait(5)
        asked.append(value)
        return {"id": value, "name": "Name " + value[-1], "avatar": ""} if value == GABE else None

    monkeypatch.setattr(steam_users, "fetch", fetch)
    first = client.post("/api/steam/users", json={"ids": [GABE, other, "not-an-id", GABE]}).json()
    assert first["loading"] is True and first["users"] == {GABE: {"name": "", "avatar": ""}, other: {"name": "", "avatar": ""}}
    release.set()
    deadline = time.time() + 5
    while time.time() < deadline:
        state = client.post("/api/steam/users", json={"ids": [GABE, other]}).json()
        if not state["loading"]:
            break
        time.sleep(0.02)
    assert state["users"][GABE]["name"] == "Name 0" and state["users"][other]["name"] == ""
    assert sorted(asked) == [GABE, other]  # each asked once: a failed one is not retried straight away
