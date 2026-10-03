import io
import zipfile

import pytest
from fastapi.testclient import TestClient

PASSWORD = "correct-horse-battery"
SETTINGS = (
    "[EngineVersionOverride]\nMajorVersion = \nMinorVersion = \nDebugBuild =\n"
    "[Debug]\nConsoleEnabled = 1\nGuiConsoleEnabled = 1\nGuiConsoleVisible = 1\nGraphicsAPI = opengl\n"
    "[CrashDump]\nEnableDumping = 1\n"
)
CONFIG_SETTINGS = "[EngineVersionOverride]\nMajorVersion = 5\nMinorVersion = 6\nDebugBuild = 0\n[Debug]\nGraphicsAPI = dx11\n"
MODS_TXT = "CheatManagerEnablerMod : 1\nConsoleEnablerMod : 1\nBPModLoaderMod : 1\n\n; Built-in keybinds, do not move up!\nKeybinds : 1\n"


def make_zip(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def framework_zip(prefix: str = "") -> bytes:
    files = {
        "dwmapi.dll": b"proxy",
        "ue4ss/UE4SS.dll": b"ue4ss",
        "ue4ss/UE4SS-settings.ini": SETTINGS,
        "ue4ss/Mods/mods.txt": MODS_TXT,
        "README.md": "ignored",
    }
    for builtin in ("CheatManagerEnablerMod", "ConsoleEnablerMod", "BPModLoaderMod", "Keybinds"):
        files[f"ue4ss/Mods/{builtin}/Scripts/main.lua"] = "-- builtin"
    return make_zip({prefix + k: v for k, v in files.items()})


def game_configs_zip() -> bytes:
    return make_zip({
        "Vein/UE4SS-settings.ini": CONFIG_SETTINGS,
        "Vein/MemberVariableLayout.ini": "[UEnum]\nCppForm = 0x50\n",
        "Vein/VTableLayout.ini": "[FMalloc]\n",
        "Vein/UE4SS_Signatures/FText_Constructor.lua": "-- signature",
        "OtherGame/MemberVariableLayout.ini": "[Nope]\n",
    })


def upload(client, path, name, data):
    return client.post(path, files={"file": (name, data)})


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", PASSWORD)
    from vsm.app import create_app

    with TestClient(create_app()) as c:
        c.post("/api/auth/login", json={"password": PASSWORD})
        m = c.app.state.manager
        m.ue4ss.supported = True  # the file handling is identical on every platform
        m.paths.ue4ss_root.mkdir(parents=True)  # as if the Windows server were installed
        # Stand in for GitHub: the release offers a build and the per-game configs.
        m.ue4ss.downloads = []
        monkeypatch.setattr(m.ue4ss, "_release_assets", lambda: {"UE4SS_v9.9.9.zip": "build", "zCustomGameConfigs.zip": "configs"})

        def download(url):
            m.ue4ss.downloads.append(url)
            return io.BytesIO(framework_zip() if url == "build" else game_configs_zip())

        monkeypatch.setattr(m.ue4ss, "_download", download)
        yield c


@pytest.fixture
def installed(client):
    assert upload(client, "/api/ue4ss/upload", "UE4SS_v3.0.1-1152-ge3ba1016.zip", framework_zip()).status_code == 200
    return client


def names(mods):
    return [m["name"] for m in mods]


def test_unsupported_platform_is_refused(client):
    client.app.state.manager.ue4ss.supported = False
    assert client.get("/api/ue4ss").json()["supported"] is False
    assert upload(client, "/api/ue4ss/upload", "x.zip", framework_zip()).status_code == 409
    assert client.get("/api/logic-mods").status_code == 409
    assert client.get("/api/mods").status_code == 200  # pak mods work everywhere


def test_install_from_zip(installed):
    m = installed.app.state.manager
    s = installed.get("/api/ue4ss").json()
    assert s["installed"] and s["enabled"] and s["version"] == "v3.0.1-1152-ge3ba1016" and s["source"] == "upload"
    assert (m.paths.ue4ss_root / "dwmapi.dll").read_bytes() == b"proxy"
    assert not (m.paths.ue4ss_root / "README.md").exists()  # only the proxy and ue4ss/ are extracted
    settings = (m.paths.ue4ss_dir / "UE4SS-settings.ini").read_text()
    assert "ConsoleEnabled = 0" in settings and "GuiConsoleEnabled = 0" in settings and "GuiConsoleVisible = 0" in settings
    assert "GraphicsAPI = opengl" in settings  # only the engine override is taken from the game config
    assert "EnableDumping = 0" in settings  # no modal crash dialog on a headless machine
    # client-only built-ins are switched off, the blueprint loader stays on, Keybinds stays last
    assert {x["name"]: x["enabled"] for x in s["mods"]} == {
        "CheatManagerEnablerMod": False, "ConsoleEnablerMod": False, "BPModLoaderMod": True, "Keybinds": False}
    assert names(s["mods"])[-1] == "Keybinds"


def test_vein_game_config_is_applied(installed):
    m = installed.app.state.manager
    assert installed.get("/api/ue4ss").json()["game_config"] is True
    assert (m.paths.ue4ss_dir / "MemberVariableLayout.ini").read_text().startswith("[UEnum]")
    assert (m.paths.ue4ss_dir / "VTableLayout.ini").exists()
    assert (m.paths.ue4ss_dir / "UE4SS_Signatures" / "FText_Constructor.lua").exists()
    assert not (m.paths.ue4ss_dir / "OtherGame").exists()
    settings = (m.paths.ue4ss_dir / "UE4SS-settings.ini").read_text()
    assert "MajorVersion = 5" in settings and "MinorVersion = 6" in settings and "DebugBuild = 0" in settings


def test_install_from_github_uses_release_build_and_config(client):
    s = client.post("/api/ue4ss/install").json()
    assert s["installed"] and s["version"] == "v9.9.9" and s["source"] == "github" and s["game_config"]
    assert client.app.state.manager.ue4ss.downloads == ["build", "configs"]


def test_vein_prepackaged_zip_needs_no_download(client):
    m = client.app.state.manager
    packaged = zipfile.ZipFile(io.BytesIO(framework_zip()))
    files = {n: packaged.read(n) for n in packaged.namelist()}
    files["ue4ss/MemberVariableLayout.ini"] = "[UEnum]\nCppForm = 0x99\n"
    assert upload(client, "/api/ue4ss/upload", "UE4SS-for-Vein.zip", make_zip(files)).status_code == 200
    assert m.ue4ss.downloads == []
    assert "0x99" in (m.paths.ue4ss_dir / "MemberVariableLayout.ini").read_text()


def test_nothing_is_installed_when_the_game_config_is_unavailable(client, monkeypatch):
    from vsm import VsmError

    m = client.app.state.manager

    def offline():
        raise VsmError("Could not reach GitHub", 502)

    monkeypatch.setattr(m.ue4ss, "_release_assets", offline)
    assert upload(client, "/api/ue4ss/upload", "generic.zip", framework_zip()).status_code == 502
    assert not m.paths.ue4ss_proxy.exists() and not m.paths.ue4ss_dir.exists()


def test_install_accepts_nested_repack_and_rejects_others(client):
    assert upload(client, "/api/ue4ss/upload", "repack.zip", framework_zip("Vein/Binaries/Win64/")).status_code == 200
    assert client.get("/api/ue4ss").json()["installed"]
    flat = make_zip({"dwmapi.dll": b"p", "UE4SS.dll": b"old layout"})
    assert upload(client, "/api/ue4ss/upload", "old.zip", flat).status_code == 400
    assert upload(client, "/api/ue4ss/upload", "notzip.zip", b"hello").status_code == 400


def test_safe_mode_and_update_keeps_user_files(installed):
    m = installed.app.state.manager
    assert installed.post("/api/ue4ss/enabled", json={"enabled": False}).json()["enabled"] is False
    assert not m.paths.ue4ss_proxy.exists() and (m.paths.ue4ss_root / "dwmapi.dll.disabled").exists()

    (m.paths.ue4ss_dir / "UE4SS-settings.ini").write_text(SETTINGS.replace("opengl", "dx11").replace("= 1", "= 0"))
    before = m.ue4ss.mods_txt.read_text()
    s = upload(installed, "/api/ue4ss/upload", "UE4SS_v3.0.1-1200-gabcdef12.zip", framework_zip()).json()
    assert s["version"] == "v3.0.1-1200-gabcdef12"
    assert s["enabled"] is False and not m.paths.ue4ss_proxy.exists()  # an update does not undo safe mode
    assert "GraphicsAPI = dx11" in (m.paths.ue4ss_dir / "UE4SS-settings.ini").read_text()
    assert m.ue4ss.mods_txt.read_text() == before

    assert installed.post("/api/ue4ss/enabled", json={"enabled": True}).json()["enabled"] is True
    assert m.paths.ue4ss_proxy.exists() and not (m.paths.ue4ss_root / "dwmapi.dll.disabled").exists()


def test_script_mods(installed):
    m = installed.app.state.manager
    lua = make_zip({"BetterStacks/Scripts/main.lua": "print('hi')", "BetterStacks/enabled.txt": ""})
    r = upload(installed, "/api/ue4ss/mods", "BetterStacks.zip", lua).json()
    assert r["installed"] == ["BetterStacks"]
    # nested the way Nexus downloads often are, and a root-level mod named after its zip
    upload(installed, "/api/ue4ss/mods", "whatever.zip", make_zip({"ue4ss/Mods/ServerTweaks/dlls/main.dll": b"MZ"}))
    upload(installed, "/api/ue4ss/mods", "AdminTools.zip", make_zip({"Scripts/main.lua": "x", "Scripts/util.lua": "y"}))

    mods = {x["name"]: x for x in installed.get("/api/ue4ss").json()["mods"]}
    assert mods["BetterStacks"]["kind"] == "lua" and mods["BetterStacks"]["enabled"] and not mods["BetterStacks"]["builtin"]
    assert mods["ServerTweaks"]["kind"] == "cpp" and mods["AdminTools"]["kind"] == "lua"
    assert (m.ue4ss.mods_dir / "AdminTools" / "Scripts" / "util.lua").exists()
    assert not (m.ue4ss.mods_dir / "BetterStacks" / "enabled.txt").exists()  # mods.txt is authoritative

    # reorder and disable; Keybinds is forced last whatever the client sends
    order = [{"name": "Keybinds", "enabled": True}, {"name": "AdminTools", "enabled": True}, {"name": "BetterStacks", "enabled": False}]
    result = installed.put("/api/ue4ss/mods", json=order).json()
    assert names(result)[:2] == ["AdminTools", "BetterStacks"] and names(result)[-1] == "Keybinds"
    text = m.ue4ss.mods_txt.read_text()
    assert "AdminTools : 1\nBetterStacks : 0\n" in text and text.rstrip().endswith("Keybinds : 1")

    assert installed.delete("/api/ue4ss/mods/BetterStacks").status_code == 200
    assert "BetterStacks" not in m.ue4ss.mods_txt.read_text()
    assert installed.delete("/api/ue4ss/mods/BPModLoaderMod").status_code == 400
    assert installed.delete("/api/ue4ss/mods/..%5C..%5CUE4SS.dll").status_code in (400, 404)


def test_mod_uploads_are_validated(installed):
    m = installed.app.state.manager
    assert upload(installed, "/api/ue4ss/mods", "empty.zip", make_zip({"readme.txt": "no mod here"})).status_code == 400
    assert upload(installed, "/api/ue4ss/mods", "x.zip", b"not a zip").status_code == 400
    # a mod whose files try to climb out of its own folder
    evil = make_zip({"Evil/Scripts/main.lua": "x", "Evil/../../../../escaped.txt": "x"})
    assert upload(installed, "/api/ue4ss/mods", "evil.zip", evil).status_code == 400
    assert not (m.paths.server / "escaped.txt").exists() and not (m.ue4ss.mods_dir / "Evil").exists()
    # replacing a built-in is refused
    assert upload(installed, "/api/ue4ss/mods", "b.zip", make_zip({"BPModLoaderMod/Scripts/main.lua": "x"})).status_code == 400


def test_remove_keeps_script_mods(installed):
    m = installed.app.state.manager
    upload(installed, "/api/ue4ss/mods", "AdminTools.zip", make_zip({"Scripts/main.lua": "x"}))
    s = installed.delete("/api/ue4ss").json()
    assert s["installed"] is False and not m.paths.ue4ss_proxy.exists() and not (m.paths.ue4ss_dir / "UE4SS.dll").exists()
    assert (m.ue4ss.mods_dir / "AdminTools" / "Scripts" / "main.lua").exists()


def test_build_change_warning_and_log(installed):
    m = installed.app.state.manager
    assert installed.get("/api/ue4ss").json()["build_changed"] is False
    m.paths.manifest.parent.mkdir(parents=True, exist_ok=True)
    state = m.paths.ue4ss_state.read_text().replace('"game_build": null', '"game_build": "111"')
    m.paths.ue4ss_state.write_text(state)
    m.paths.manifest.write_text('"buildid"\t\t"222"')
    assert installed.get("/api/ue4ss").json()["build_changed"] is True

    assert installed.get("/api/ue4ss/log").json() == []
    (m.paths.ue4ss_dir / "UE4SS.log").write_text("[Lua] Starting Lua mod 'AdminTools'\n")
    assert installed.get("/api/ue4ss/log").json() == ["[Lua] Starting Lua mod 'AdminTools'"]


def test_blueprint_mods_and_pak_companions(installed):
    m = installed.app.state.manager
    for name in ("Cool_P.pak", "Cool_P.utoc", "Cool_P.ucas"):
        assert upload(installed, "/api/logic-mods", name, b"data").status_code == 200
    assert upload(installed, "/api/logic-mods", "bad.exe", b"x").status_code == 400
    assert installed.get("/api/logic-mods").json() == [{"name": "Cool_P.pak", "enabled": True, "size": 12}]
    assert installed.post("/api/logic-mods/Cool_P.pak/toggle").json()["name"] == "Cool_P.pak.disabled"
    assert sorted(p.name for p in m.paths.logic_mods.iterdir()) == ["Cool_P.pak.disabled", "Cool_P.ucas.disabled", "Cool_P.utoc.disabled"]
    assert installed.post("/api/logic-mods/Cool_P.pak.disabled/toggle").json()["name"] == "Cool_P.pak"
    assert installed.delete("/api/logic-mods/Cool_P.pak").status_code == 200
    assert list(m.paths.logic_mods.iterdir()) == []
