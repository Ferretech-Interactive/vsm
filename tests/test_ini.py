import pytest

from vsm import VsmError
from vsm.game.ini import IniFile

SAMPLE = """[/Script/Vein.VeinGameSession]
ServerName=Old
SuperAdminSteamIDs=76561198000000001
+SuperAdminSteamIDs=76561198000000002

[SomeMod.Settings]
; a comment
Rate=1.5
+Items=A
+Items=B
"""


@pytest.fixture
def ini(tmp_path):
    path = tmp_path / "Game.ini"
    path.write_text(SAMPLE, encoding="utf-8")
    return IniFile(path)


def test_read(ini):
    assert ini.sections() == ["/Script/Vein.VeinGameSession", "SomeMod.Settings"]
    assert ini.get("/script/vein.veingamesession", "servername") == "Old"
    assert ini.get_array("/Script/Vein.VeinGameSession", "SuperAdminSteamIDs") == ["76561198000000001", "76561198000000002"]
    assert ini.get("Missing", "x", "d") == "d"


def test_set_preserves_everything_else(ini):
    ini.set("/Script/Vein.VeinGameSession", "ServerName", "New")
    ini.set("/Script/Vein.VeinGameSession", "Password", "pw")
    ini.set("Brand.New", "Key", "1")
    ini.save()
    text = ini.path.read_text(encoding="utf-8")
    assert "ServerName=New" in text and "ServerName=Old" not in text
    # new key lands inside its section, not after the blank line
    assert "+SuperAdminSteamIDs=76561198000000002\nPassword=pw\n\n[SomeMod.Settings]" in text
    assert "; a comment\nRate=1.5\n+Items=A\n+Items=B" in text
    assert text.endswith("[Brand.New]\nKey=1\n")


def test_arrays(ini):
    ini.set_array("/Script/Vein.VeinGameSession", "SuperAdminSteamIDs", ["1", "2", "3"])
    assert ini.entries("/Script/Vein.VeinGameSession")[1:] == [
        ("SuperAdminSteamIDs", "1"), ("+SuperAdminSteamIDs", "2"), ("+SuperAdminSteamIDs", "3")
    ]
    ini.set_array("/Script/Vein.VeinGameSession", "SuperAdminSteamIDs", [])
    assert ini.get_array("/Script/Vein.VeinGameSession", "SuperAdminSteamIDs") == []


def test_replace_section(ini):
    ini.replace_section("SomeMod.Settings", [("Rate", "2"), ("+Items", "C")])
    assert ini.entries("SomeMod.Settings") == [("Rate", "2"), ("+Items", "C")]
    assert ini.get("/Script/Vein.VeinGameSession", "ServerName") == "Old"


def test_rejects_injection(ini):
    with pytest.raises(VsmError):
        ini.set("A", "Key", "x\n[Evil]\nK=1")
    with pytest.raises(VsmError):
        ini.set("A", "K=1\nB", "x")
