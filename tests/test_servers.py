import json

import pytest

from vsm.desktop.servers import LOCAL_ID, ServerError, ServerList, is_unencrypted_public, normalise

LOCAL = "http://127.0.0.1:8810"


@pytest.mark.parametrize("typed, expected", [
    ("192.168.1.20", "http://192.168.1.20:8810"),
    ("  vein.example.com:9000 ", "http://vein.example.com:9000"),
    ("http://Server-PC", "http://server-pc:8810"),
    ("https://vein.example.com", "https://vein.example.com"),
    ("https://vein.example.com:8443/", "https://vein.example.com:8443"),
    ("http://10.0.0.5:80", "http://10.0.0.5"),
    ("[fd00::1]:8810", "http://[fd00::1]:8810"),
])
def test_normalise(typed, expected):
    assert normalise(typed) == expected


@pytest.mark.parametrize("typed", [
    "", "ftp://host", "javascript:alert(1)", "file:///C:/x", "http://", "http://user:pw@host",
    "http://host/some/path", "http://host?x=1", "http://host:99999", "http://host#frag",
])
def test_normalise_rejects(typed):
    with pytest.raises(ServerError):
        normalise(typed)


@pytest.mark.parametrize("url, warn", [
    ("http://192.168.1.20:8810", False),
    ("http://10.1.2.3:8810", False),
    ("http://127.0.0.1:8810", False),
    ("http://server-pc:8810", False),
    ("http://nas.local:8810", False),
    ("http://8.8.8.8:8810", True),
    ("http://vein.example.com:8810", True),
    ("https://vein.example.com", False),
])
def test_unencrypted_public_warning(url, warn):
    assert is_unencrypted_public(url) is warn


def test_list_round_trip(tmp_path):
    path = tmp_path / "servers.json"
    servers = ServerList(path, LOCAL)
    assert [s["id"] for s in servers.entries()] == [LOCAL_ID] and servers.initial()["id"] == LOCAL_ID
    a = servers.add("Weekend", "192.168.1.20")
    b = servers.add("Friends", "https://vein.example.com")
    servers.update(a["id"], "Weekend PvP", "192.168.1.21:9000")
    servers.remember(b["id"])

    again = ServerList(path, LOCAL)
    assert [(s["name"], s["url"]) for s in again.entries()] == [
        ("This PC", LOCAL), ("Weekend PvP", "http://192.168.1.21:9000"), ("Friends", "https://vein.example.com")]
    assert again.initial()["id"] == b["id"]
    again.remove(b["id"])
    assert again.initial()["id"] == LOCAL_ID and len(ServerList(path, LOCAL).entries()) == 2
    assert "password" not in path.read_text().lower()


def test_this_pc_is_fixed_and_bad_input_is_refused(tmp_path):
    servers = ServerList(tmp_path / "servers.json", LOCAL)
    with pytest.raises(ServerError):
        servers.remove(LOCAL_ID)
    with pytest.raises(ServerError):
        servers.update(LOCAL_ID, "x", "1.2.3.4")
    with pytest.raises(ServerError):
        servers.add("", "1.2.3.4")
    with pytest.raises(ServerError):
        servers.add("x" * 61, "1.2.3.4")
    with pytest.raises(ServerError):
        servers.add("Bad", "ftp://host")
    assert len(servers.entries()) == 1


def test_client_only_and_damaged_file(tmp_path):
    path = tmp_path / "servers.json"
    client = ServerList(path, None)
    assert client.entries() == [] and client.initial() is None
    path.write_text(json.dumps({"servers": [{"id": "a", "name": "Ok", "url": "1.2.3.4"}, {"id": "b", "name": "Bad", "url": "file:///x"}, "junk"], "last": 5}))
    assert [s["name"] for s in ServerList(path, None).entries()] == ["Ok"]
    path.write_text("not json")
    assert ServerList(path, None).entries() == []


@pytest.mark.parametrize("page, local", [
    ("http://127.0.0.1:8810/", True),
    ("http://127.0.0.1:8810/#/dashboard", True),
    ("http://127.0.0.1:8810", True),
    ("http://127.0.0.1:8811/", False),  # another port on this PC
    ("https://127.0.0.1:8810/", False),  # different scheme
    ("http://127.0.0.1.evil.example:8810/", False),  # look-alike host
    ("http://evil.example/127.0.0.1:8810/", False),
    ("http://127.0.0.1:8810@evil.example/", False),  # the real host is evil.example
    ("http://192.168.1.20:8810/", False),
    ("", False),
    ("about:blank", False),
])
def test_local_sign_in_is_only_offered_to_this_pcs_manager(page, local):
    pytest.importorskip("webview")
    from vsm.desktop.shell import is_local_origin

    assert is_local_origin(page, LOCAL) is local
    assert is_local_origin(page, None) is False


def test_window_address_says_it_is_the_app():
    pytest.importorskip("webview")
    from vsm.desktop.shell import is_local_origin, window_url

    assert window_url("http://127.0.0.1:8810") == "http://127.0.0.1:8810/?desktop=1"
    assert window_url("https://vein.example.com:9000/") == "https://vein.example.com:9000/?desktop=1"
    assert is_local_origin(window_url(LOCAL), LOCAL) is True  # the marker does not change whose page it is
