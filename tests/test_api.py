import io
import zipfile

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    monkeypatch.setenv("VSM_SERVER_NAME", "Env Server")
    monkeypatch.setenv("VSM_MAX_PLAYERS", "24")
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def authed(client):
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200
    return client


def test_requires_auth(client):
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/config").status_code == 401
    assert client.post("/api/server/start").status_code == 401
    assert client.get("/api/auth/state").json()["authenticated"] is False
    assert client.get("/api/auth/state").json()["service_mode"] is False


def test_login_lockout(client):
    for _ in range(5):
        assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 429


def test_cross_origin_refused(authed):
    r = authed.post("/api/backups", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_env_seeding_and_status(authed):
    assert authed.get("/api/game-settings").json()["server_name"] == "Env Server"
    assert authed.get("/api/config").json()["launch"]["max_players"] == 24
    s = authed.get("/api/status").json()
    assert s["state"] == "OFFLINE" and s["installed"] is False


def test_config_validation_and_secret_masking(authed):
    assert authed.put("/api/config", json={"launch": {"port": 99999}}).status_code == 400
    assert authed.put("/api/config", json={"discord": {"webhook_url": "http://169.254.169.254/"}}).status_code == 400
    r = authed.put("/api/config", json={"rcon": {"password": "s3cret"}, "schedule": {"times": ["23:30", "04:00"]}})
    assert r.json()["rcon"]["password"] == "********"
    assert r.json()["schedule"]["times"] == ["04:00", "23:30"]
    # echoing the mask back must not overwrite the stored secret
    authed.put("/api/config", json={"rcon": {"password": "********", "port": 27021}})
    assert authed.app.state.manager.config.get().rcon.password == "s3cret"


def test_game_settings_and_gameplay(authed):
    r = authed.put("/api/game-settings", json={"server_name": "My Server", "password": "pw_1", "super_admins": ["76561198000000001, 76561198000000002"], "http_port": 8080})
    assert r.status_code == 200
    assert r.json()["super_admins"] == ["76561198000000001", "76561198000000002"]
    assert authed.put("/api/game-settings", json={"super_admins": ["123"]}).status_code == 400
    assert authed.put("/api/game-settings", json={"password": "has space"}).status_code == 400

    g = authed.get("/api/gameplay").json()
    assert g["values"]["vein.Zombies.Health"] == "40" and g["values"]["vein.PvP"] is True
    r = authed.put("/api/gameplay", json={"vein.Zombies.Health": "75", "vein.PvP": False, "vein.Movement.WalkSpeed": "300", "GS_HungerMultiplier": "1.5"})
    assert r.json()["vein.Zombies.Health"] == "75" and r.json()["vein.PvP"] is False
    assert authed.put("/api/gameplay", json={"vein.Zombies.Health": "1\n[x]"}).status_code == 400

    paths = authed.app.state.manager.paths
    assert "vein.PvP=0" in paths.engine_ini.read_text()
    assert "vein.Movement.WalkSpeed=300" in paths.engine_ini.read_text()
    assert "GS_HungerMultiplier=1.5" in paths.game_ini.read_text()
    authed.put("/api/gameplay", json={"GS_ShowScoreboardBadges": False})
    assert "GS_ShowScoreboardBadges=0" in paths.game_ini.read_text()
    assert authed.get("/api/gameplay").json()["values"]["GS_ShowScoreboardBadges"] is False


def test_gameplay_defaults_match_the_game():
    """Values the dedicated server logged for its own settings, with no overrides (build 25708939)."""
    from vsm.game.gameplay import rows

    logged = {
        "vein.AllowPickpocketing": 1, "vein.TV.Server.AllowRemoteContent": 1, "vein.AlwaysBecomeZombie": 0, "vein.BaseDamage": 1,
        "vein.UtilityCabinet.AllowRaiding": 1, "vein.BuildObjectPvP": 1, "vein.BuildObjectDecay": 1, "vein.ClothingHideable": 0,
        "vein.ContainersRespawn.Enabled": 1, "vein.Movement.CrouchWalkSpeed": 200, "vein.Furniture.Respawns": 1,
        "vein.HeadshotDamageMultiplier": 2, "vein.AISpawner.Hordes.Enabled": 1, "vein.Placement.IgnorePlacementRestrictions": 0,
        "vein.ItemActorSpawner.Respawns": 1, "vein.Characters.Max": 100, "vein.Wire.TVMaxRadius": 5000, "vein.Placement.MaxUtilityCabinets": 0,
        "vein.Wire.MaxRadius": 1500, "vein.Time.NightTimeMultiplier": 3, "vein.NoSaves": 0, "vein.OfflineRaidProtection": 0, "vein.Permadeath": 0,
        "vein.PersistentCorpses": 1, "vein.Calendar.ElectricalShutoffTimeDays": 46, "vein.PvP": 1, "vein.RetainItemsOnDeath": 0,
        "vein.Movement.RunExtraSpeed": 350, "vein.Scarcity.Difficulty": 2, "vein.Sleep.AdvanceTime": 0, "vein.StaggerChance": 0.1,
        "vein.Time.StartOffsetDays": 0, "vein.StunLockChance": 0.6, "vein.StunLockDuration": 2, "vein.Movement.SwimSpeed": 300,
        "vein.Time.TimeMultiplier": 16, "vein.Time.ContinueWithNoPlayers": 0, "vein.UtilityCabinet.Interval": 4,
        "vein.UtilityCabinet.MaxPylonDistance": 5000, "vein.UtilityCabinet.MaxPylons": 6, "vein.Vehicles.Damage.OutgoingPlayerDamage": 1,
        "vein.Movement.WalkSpeed": 250, "vein.Calendar.WaterShutoffTimeDays": 30, "vein.Zombies.CrawlSpeedMultiplier": 1,
        "vein.Zombies.DamageMultiplier": 1, "vein.Zombies.HearingMultiplier": 1, "vein.ZombieInfectionChance": 0.01,
        "vein.Zombies.RunSpeedMultiplier": 1, "vein.Zombies.CanClimb": 1, "vein.Zombies.HeadshotOnly": 0, "vein.Zombies.Health": 40,
        "vein.Zombies.SightMultiplier": 1, "vein.AISpawner.SpawnCapMultiplierZombie": 1, "vein.Zombies.SpeedMultiplier": 1,
        "vein.AISpawner.ZombieWalkerPercentage": 0.8, "vein.Zombies.WalkSpeedMultiplier": 1,
    }
    # Settings with no console variable behind them: the server logs "Initialized from default value".
    logged.update({"GS_HungerMultiplier": 1, "GS_ThirstMultiplier": 1, "GS_TiredMultiplier": 1, "GS_MaxThirdPersonDistance": 400, "GS_ShowScoreboardBadges": 1})
    ours = {key: default for _label, key, _help, _type, _file, _section, default in rows()}
    assert not set(logged) - set(ours), "a setting the game lists is missing from the Gameplay page"
    wrong = {k: (ours[k], v) for k, v in logged.items() if float(ours[k]) != float(v)}
    assert not wrong, wrong
    keys = [key for _label, key, *_ in rows()]
    assert len(keys) == len(set(keys))
    for _label, key, _help, _type, file, section, _default in rows():
        assert (file, section) == (("Game", "/Script/Vein.ServerSettings") if key.startswith("GS_") else ("Engine", "ConsoleVariables")), key


def test_ini_editor_protects_core_sections(authed):
    assert authed.put("/api/ini/section", json={"section": "/Script/Vein.VeinGameSession", "entries": [["Password", "x"]]}).status_code == 400
    assert authed.put("/api/ini/section", json={"section": "My.Mod", "entries": [["A", "1"], ["+B", "2"]]}).status_code == 200
    assert "My.Mod" in authed.get("/api/ini/sections").json()
    assert authed.get("/api/ini/section", params={"name": "My.Mod"}).json() == [["A", "1"], ["+B", "2"]]


def test_mods(authed):
    assert authed.post("/api/mods", files={"file": ("evil.exe", b"x")}).status_code == 400
    assert authed.post("/api/mods", files={"file": ("../../escape.pak", b"x")}).json()["name"] == "escape.pak"
    assert authed.post("/api/mods", files={"file": ("Cool_P.pak", b"data")}).status_code == 200
    assert authed.post("/api/mods/Cool_P.pak/toggle").json()["name"] == "Cool_P.pak.disabled"
    mods = {m["name"]: m["enabled"] for m in authed.get("/api/mods").json()}
    assert mods == {"Cool_P.pak.disabled": False, "escape.pak": True}
    assert authed.delete("/api/mods/..%5C..%5Cconfig.json").status_code in (400, 404)
    assert authed.delete("/api/mods/escape.pak").status_code == 200


def test_bans(authed):
    assert authed.post("/api/bans", json={"steam_id": "76561198000000009"}).json() == ["76561198000000009"]
    assert authed.post("/api/bans", json={"steam_id": "bad"}).status_code == 400
    assert authed.delete("/api/bans/76561198000000009").json() == []


def test_bans_made_while_running_survive_the_server_rewriting_its_ini(authed):
    m = authed.app.state.manager
    m.server.pid = 4242  # a game server is running
    try:
        assert authed.post("/api/bans", json={"steam_id": "76561198000000009"}).json() == ["76561198000000009"]
    finally:
        m.server.pid = None
    m.paths.game_ini.write_text("[/Script/Vein.VeinGameStateBase]\n", encoding="utf-8")  # what the server does on exit
    assert authed.get("/api/players").json()["bans"] == ["76561198000000009"]  # still shown
    m.game.flush_bans()  # the controller calls this once the server has stopped
    assert "76561198000000009" in m.paths.game_ini.read_text(encoding="utf-8")
    assert authed.get("/api/players").json()["bans"] == ["76561198000000009"]


def test_backups_and_profiles(authed):
    m = authed.app.state.manager
    (m.paths.saved / "SaveGames").mkdir(parents=True)
    (m.paths.saved / "SaveGames" / "Server.vns").write_bytes(b"world")
    (m.paths.saved / "Logs").mkdir()
    (m.paths.saved / "Logs" / "Vein.log").write_text("noise")
    name = authed.post("/api/backups").json()["name"]
    with zipfile.ZipFile(io.BytesIO(authed.get(f"/api/backups/{name}").content)) as z:
        names = z.namelist()
        assert "SaveGames/Server.vns" in names and not any(n.startswith("Logs") for n in names)
    (m.paths.saved / "SaveGames" / "Server.vns").write_bytes(b"corrupted")
    assert authed.post(f"/api/backups/{name}/restore").status_code == 200
    assert (m.paths.saved / "SaveGames" / "Server.vns").read_bytes() == b"world"
    assert authed.get("/api/backups/..%2Fauth.json").status_code in (400, 404)

    authed.put("/api/game-settings", json={"server_name": "Profile A"})
    assert authed.post("/api/profiles", json={"name": "pvp"}).json() == ["pvp"]
    assert authed.post("/api/profiles", json={"name": "../x"}).status_code == 400
    authed.put("/api/game-settings", json={"server_name": "Changed"})
    authed.post("/api/profiles/pvp/load")
    assert authed.get("/api/game-settings").json()["server_name"] == "Profile A"
    assert authed.delete("/api/profiles/pvp").json() == []


def test_start_without_install_reports_error(authed):
    assert authed.post("/api/server/start").status_code == 200
    import time

    for _ in range(50):
        if any("not installed" in e["message"] for e in authed.get("/api/logs").json()["events"]):
            break
        time.sleep(0.1)
    else:
        pytest.fail("expected a 'not installed' event")
    assert authed.get("/api/status").json()["state"] == "OFFLINE"


def test_websocket_requires_session(client):
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws"):
            pass
    client.post("/api/auth/login", json={"password": PASSWORD})
    with client.websocket_connect("/api/ws") as ws:
        assert ws.receive_json()["type"] == "status"


def test_network_addresses(authed, monkeypatch):
    from vsm.api import routes
    from vsm.core import network

    monkeypatch.setattr(routes, "public_ip", lambda: "8.8.8.8")
    monkeypatch.setattr(routes, "local_ip", lambda: "192.168.1.20")
    assert authed.get("/api/network").json() == {"internal_ip": "192.168.1.20", "external_ip": "8.8.8.8", "game_port": 7777}
    with network.clean_dll_search():  # a no-op outside the packaged Windows build
        pass


def test_the_games_own_discord_relay_and_single_threading(authed):
    hook = "https://discord.com/api/webhooks/123456/abc-DEF_ghi"
    r = authed.put("/api/game-settings", json={"discord_chat_webhook": hook, "discord_admin_webhook": f'"{hook}"'})
    assert r.json()["discord_chat_webhook"] == hook and r.json()["discord_admin_webhook"] == hook
    ini = authed.app.state.manager.paths.game_ini.read_text()
    assert f'DiscordChatWebhookURL="{hook}"' in ini and f'DiscordChatAdminWebhookURL="{hook}"' in ini  # quoted, as the game needs
    assert authed.put("/api/game-settings", json={"discord_chat_webhook": "https://example.com/hook"}).status_code == 400
    assert authed.put("/api/game-settings", json={"discord_chat_webhook": ""}).json()["discord_chat_webhook"] == ""
    assert "DiscordChatWebhookURL=" not in authed.app.state.manager.paths.game_ini.read_text()

    assert authed.get("/api/config").json()["launch"]["single_thread"] is False
    assert authed.put("/api/config", json={"launch": {"single_thread": True}}).json()["launch"]["single_thread"] is True
    assert authed.get("/api/diagnostics").json()["game_log"] == {"log_found": False, "findings": []}


def test_port_use_says_who_holds_a_udp_port():
    import os
    import types

    import psutil

    from vsm.api.routes import port_use

    def sock(port, pid):
        return types.SimpleNamespace(laddr=types.SimpleNamespace(port=port), pid=pid)

    me = os.getpid()
    sockets = [sock(7777, 4242), sock(27015, me), sock(9000, None), types.SimpleNamespace(laddr=None, pid=1)]
    assert port_use(7777, 4242, sockets) == {"state": "game", "program": ""}
    assert port_use(7778, 4242, sockets) == {"state": "free", "program": ""}
    other = port_use(27015, 4242, sockets)  # held by some other program, here this test's own process
    assert other == {"state": "other", "program": psutil.Process(me).name()}  # "python.exe", or "pytest" on Linux
    assert port_use(27015, None, sockets)["state"] == "other"  # no game server running: still not "listening"
    assert port_use(9000, 4242, sockets) == {"state": "other", "program": ""}  # owner unknown to us
