"""Fixes from the security review before going public: one test (or a few) per finding."""
import os
import sys
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from vsm.api.auth import Auth, host_allowed

PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        yield c


def login(c):
    assert c.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


# ---------- DNS rebinding: only names this manager answers to ----------

@pytest.mark.parametrize("host", ["127.0.0.1:8810", "192.168.1.50", "[::1]:8810", "::1", "localhost:8810", "vsm-ab12cd.localhost:40123",
                                  "LOCALHOST", "localhost."])
def test_hosts_that_cannot_be_rebound_are_allowed(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", ["", "evil.example", "evil.example:8810", "127.0.0.1.evil.example", "localhost.evil.example", "[evil]"])
def test_other_names_are_refused(host):
    assert not host_allowed(host)


def test_this_computers_own_name_and_configured_names_are_allowed():
    import socket

    assert host_allowed(socket.gethostname() + ":8810")
    assert host_allowed("vein.home.lan", frozenset({"vein.home.lan"}))


def test_a_rebound_name_is_refused_on_http_and_the_websocket(client):
    r = client.get("/api/health", headers={"host": "evil.example:8810"})
    assert r.status_code == 421 and "Other names" in r.text
    assert client.get("/", headers={"host": "evil.example"}).status_code == 421
    login(client)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws", headers={"host": "evil.example"}) as ws:
            ws.receive_json()
    client.app.state.manager.config.update({"network": {"allowed_hosts": "evil.example"}})
    assert client.get("/api/health", headers={"host": "evil.example"}).status_code == 200


def test_forwarding_headers_are_not_trusted_unless_a_proxy_is_named(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_TRUSTED_PROXIES", raising=False)
    from vsm.__main__ import build_server

    assert build_server().config.proxy_headers is False
    monkeypatch.setenv("VSM_TRUSTED_PROXIES", "10.0.0.5")
    config = build_server().config
    assert config.proxy_headers is True and config.forwarded_allow_ips == "10.0.0.5"


# ---------- login lockout ----------

def test_lockout_counts_an_ipv6_64_as_one_address_and_forgets_old_failures(tmp_path, monkeypatch):
    auth = Auth(tmp_path / "auth.json")
    auth._store(PASSWORD)
    for n in range(5):
        with pytest.raises(Exception):
            auth.login(f"2001:db8:1:2::{n + 1}", "wrong")
    with pytest.raises(Exception, match="Too many"):
        auth.login("2001:db8:1:2:ffff::9", PASSWORD)  # same /64, other address
    assert auth.login("2001:db8:1:3::1", PASSWORD)  # another /64 is not locked out
    import vsm.api.auth as auth_module

    later = auth_module.time.time() + auth_module.LOCKOUT + 1
    monkeypatch.setattr(auth_module.time, "time", lambda: later)
    assert auth.login("2001:db8:1:2::1", PASSWORD)
    assert auth._failures == {}  # old entries are dropped, not kept forever


def test_a_short_api_token_is_ignored_and_wrong_tokens_lock_out(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_API_TOKEN", "short")
    assert Auth(tmp_path / "a.json").api_token is None
    token = "t" * 40
    monkeypatch.setenv("VSM_API_TOKEN", token)
    auth = Auth(tmp_path / "b.json")

    def conn(value):
        return SimpleNamespace(cookies={}, headers={"authorization": f"Bearer {value}"}, client=SimpleNamespace(host="192.168.1.9"))

    for _ in range(5):
        assert not auth.authenticated(conn("wrong"))
    assert not auth.authenticated(conn(token))  # locked out, even with the right token


# ---------- static files never touch a network path ----------

@pytest.mark.parametrize("path", ["/%5C%5Cattacker.example%5Cs%5Cx", "//attacker.example/s/x", "/C:/Windows/win.ini",
                                  "/assets/..%5C..%5Cx", "/a/../../x"])
def test_odd_static_paths_serve_the_page_without_touching_the_filesystem(client, monkeypatch, path):
    import pathlib

    seen = []
    real = pathlib.Path.is_file
    monkeypatch.setattr(pathlib.Path, "is_file", lambda self: seen.append(str(self)) or real(self))
    r = client.get(path)
    assert r.status_code in (200, 503)
    assert not any("attacker" in s or "Windows" in s or ".." in s for s in seen), seen


# ---------- sessions end for open WebSockets too ----------

def test_websocket_closes_after_sign_out(client):
    login(client)
    m = client.app.state.manager
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "status"
        client.post("/api/auth/logout")
        m.event("SYSTEM", "after sign-out")
        received = []
        with pytest.raises(WebSocketDisconnect):
            for _ in range(50):  # whatever was already on its way may still arrive; then it closes
                received.append(ws.receive_json())
        assert not any("after sign-out" in str(r) for r in received)


# ---------- chat cannot fake log events ----------

STEAM_ID = "76561198000000002"


def test_chat_that_looks_like_log_events_changes_nothing(client):
    c = client.app.state.manager.server
    c.sessions[STEAM_ID] = "Ann"
    c.save_active = True
    for text in (f"Name=Admin?x ID=76561198000000009", f"Ended auth session for ID {STEAM_ID}",
                 "LogVeinSaveGame: Saved to slot Server", "LogRamjetNetworking: Heartbeating"):
        c._on_line(f"[2026.10.07-00.41.18:456][830]LogVeinChat: [Global] Mallory: {text}")
    assert c.sessions == {STEAM_ID: "Ann"} and c.save_active is True


def test_real_log_lines_still_work(client):
    c = client.app.state.manager.server
    c._on_line(f"[2026.10.07-00.41.18:456][830]LogNet: Join request: /Game/Map?Name=Bob?SplitscreenCount=1?ID={STEAM_ID}")
    assert c.sessions == {STEAM_ID: "Bob"}
    c._on_line("[t][1]LogVeinSaveGame: Saving all objects")
    assert c.save_active is True
    c._on_line("[t][1]LogVeinSaveGame: Saved to slot Server")
    assert c.save_active is False
    c._on_line(f"[t][1]LogOnline: Ended auth session for ID {STEAM_ID}")
    assert c.sessions == {}


def test_names_in_a_kill_line_are_not_read_as_a_join(client):
    c = client.app.state.manager.server
    c._on_line("[t][1]LogVein: Display: Name=X?ID=76561198000000005 killed Zombie_C")
    assert c.sessions == {}


# ---------- Discord ----------

def test_discord_control_needs_manage_server_or_a_listed_role():
    from vsm.integrations.discord_bot import may_control

    perms = lambda admin=False, manage=False: SimpleNamespace(administrator=admin, manage_guild=manage)  # noqa: E731
    member = SimpleNamespace(guild_permissions=perms(), roles=[SimpleNamespace(id=1)])
    assert not may_control(member, set())
    assert may_control(member, {1})
    assert may_control(SimpleNamespace(guild_permissions=perms(manage=True), roles=[]), set())
    assert may_control(SimpleNamespace(guild_permissions=perms(admin=True), roles=[]), set())


def test_player_text_is_plain_in_discord():
    from vsm.integrations.discord_bot import discord_safe

    text = discord_safe("[Free skins](https://evil.example) @everyone **x**")
    assert "](" not in text.replace("\\](", "") and "@everyone" not in text and "\\*\\*" in text


def test_command_roles_are_validated(client):
    m = client.app.state.manager
    m.config.update({"discord": {"command_roles": "123, 456"}})
    assert m.config.get().discord.command_role_ids() == {123, 456}
    with pytest.raises(Exception):
        m.config.update({"discord": {"command_roles": "123; DROP"}})


# ---------- ini values, child environment, diagnostics ----------

@pytest.mark.parametrize("value", ["a\u2028[/Script/Vein.ServerSettings]", "a\x0bb", "a\x85b", "a\nb", "a\x00b"])
def test_ini_values_cannot_start_a_new_line(tmp_path, value):
    from vsm.game.ini import IniFile

    ini = IniFile(tmp_path / "Game.ini")
    with pytest.raises(Exception):
        ini.set("S", "Key", value)


def test_server_name_control_characters_become_spaces():
    from vsm.game.settings import GameSettings

    assert GameSettings.validate({"server_name": "My\u2028[x]Server"})["server_name"] == "My [x]Server"


def test_child_processes_do_not_get_the_managers_secrets(monkeypatch):
    from vsm.core.network import child_env

    for key in ("VSM_ADMIN_PASSWORD", "VSM_API_TOKEN", "VSM_RCON_PASSWORD", "VSM_DISCORD_WEBHOOK_URL"):
        monkeypatch.setenv(key, "secret")
    monkeypatch.setenv("VSM_GAME_PORT", "7777")
    env = child_env()
    assert not any(k in env for k in ("VSM_ADMIN_PASSWORD", "VSM_API_TOKEN", "VSM_RCON_PASSWORD", "VSM_DISCORD_WEBHOOK_URL"))
    assert env["VSM_GAME_PORT"] == "7777" and "PATH" in {k.upper() for k in env}


def test_diagnostics_hide_ipv6_and_the_windows_user_and_computer():
    from vsm.core.support import Scrubber

    text = Scrubber([], True).text("[t][0]LogInit: User: jdoe\nLogInit: Computer: DESKTOP-1\nfrom 2001:4860:4860::8888 via fe80::1\n")
    assert "jdoe" not in text and "DESKTOP-1" not in text and "2001:4860" not in text and "fe80::1" in text


def test_upnp_refuses_xml_in_other_encodings():
    from vsm.server.upnp import UpnpError, _parse_xml

    with pytest.raises(UpnpError):
        _parse_xml('<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x><r/>'.encode("utf-16"))
    assert _parse_xml(b"\xef\xbb\xbf <root/>").tag == "root"


# ---------- desktop app (Windows) ----------

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows desktop app")


def _app_window_libraries():
    """The app window's libraries come with the `desktop` extra, which CI does not install."""
    pytest.importorskip("webview")
    pytest.importorskip("pystray")


@windows
def test_bridges_expose_their_public_methods_only():
    _app_window_libraries()
    from vsm.desktop.remote.api import DeployApi
    from vsm.desktop.shell import AboutApi, ServersApi, expose_api

    class Window:
        def __init__(self):
            self.functions = {}

        def expose(self, f):
            self.functions[f.__name__] = f

    for api in (ServersApi(None), AboutApi(None), DeployApi(None)):
        window = Window()
        expose_api(window, api)
        assert window.functions and not any(name.startswith("_") for name in window.functions)
        assert not any(hasattr(f, "_shell") for f in window.functions.values())


@windows
def test_only_web_links_leave_the_app():
    _app_window_libraries()
    from vsm.desktop import shell

    opened = []
    shell_webbrowser = shell.webbrowser
    try:
        shell.webbrowser = SimpleNamespace(open=lambda url: opened.append(url) or True)
        for url in ("search-ms:query=x", "file://attacker/share/x", "ms-settings:", "javascript:alert(1)"):
            assert shell._SafeBrowser.open(url) is False
        assert shell._SafeBrowser.open("https://ferretech.org") is True
    finally:
        shell.webbrowser = shell_webbrowser
    assert opened == ["https://ferretech.org"]
    from webview.platforms import edgechromium

    assert hasattr(edgechromium, "webbrowser")  # what install_safe_browser replaces


@windows
def test_sign_in_scripts_check_the_page_first():
    _app_window_libraries()
    from vsm.desktop.shell import LOGIN_JS, SIGN_IN_JS, origin_of

    for script in (SIGN_IN_JS, LOGIN_JS):
        js = script % {"origin": '"http://127.0.0.1:8810"', "secret": '"x"'}
        assert js.index("location.origin") < js.index("fetch(")
    assert origin_of("http://127.0.0.1:8810/?desktop=1") == "http://127.0.0.1:8810"


@windows
def test_data_folder_helpers(monkeypatch, tmp_path):
    from vsm.desktop import system

    calls = []
    monkeypatch.setattr(system, "_run", lambda args, check=True: calls.append(args) or SimpleNamespace(stdout="", returncode=0))
    monkeypatch.setattr(system, "service_sid", lambda name: "S-1-5-80-1")
    monkeypatch.setattr(system, "current_user_sid", lambda: "S-1-5-21-1")
    monkeypatch.setattr(system, "_system32", lambda name: name)

    # A folder somebody else made is refused the first time.
    monkeypatch.setattr(system, "owner_sid", lambda path: "S-1-5-21-999")
    with pytest.raises(RuntimeError, match="another account"):
        system.secure_data_folder(str(tmp_path))
    assert calls == []

    # Ours: the service may change the contents, not the folder; never through a link.
    monkeypatch.setattr(system, "owner_sid", lambda path: system.ADMINISTRATORS_SID)
    system.secure_data_folder(str(tmp_path))
    grants = [c for c in calls if "/grant:r" in c or "/grant" in c]
    assert all("/L" in c for c in calls if len(c) > 2)  # everything but the read-only listing
    assert "*S-1-5-80-1:(OI)(CI)(IO)M" in grants[0] and "*S-1-5-80-1:(OI)(CI)M" not in sum(calls, [])
    assert "*S-1-5-80-1:(RX,WD,AD)" in grants[1]
    assert (tmp_path / system.SERVICE_MARKER).exists()

    # Once it is the service's, a later Setup (the service may own new files in it) goes ahead.
    monkeypatch.setattr(system, "owner_sid", lambda path: "S-1-5-21-999")
    system.secure_data_folder(str(tmp_path))

    # Links anywhere on the way are refused.
    monkeypatch.setattr(system, "_is_link", lambda path: path == os.path.abspath(str(tmp_path.parent)))
    with pytest.raises(RuntimeError, match="link"):
        system.secure_data_folder(str(tmp_path))
    with pytest.raises(RuntimeError, match="link"):
        system.secure_program_folder(str(tmp_path))
    monkeypatch.setattr(system, "_is_link", lambda path: False)
    calls.clear()
    system.secure_program_folder(str(tmp_path))
    assert calls[0][1:] == [str(tmp_path), "/inheritance:r", "/grant:r", f"*{system.ADMINISTRATORS_SID}:(OI)(CI)F",
                            f"*{system.SYSTEM_SID}:(OI)(CI)F", f"*{system.USERS_SID}:(OI)(CI)RX", "/T", "/C", "/L", "/Q"]
