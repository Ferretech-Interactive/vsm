import json
import os

import pytest

from vsm.desktop import firewall
from vsm.desktop.firewall import FirewallError, Rule


def write(path, data):
    path.write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
    return path


def config(tmp_path, port=7777, query=27015, **extra):
    return write(tmp_path / "config.json", {"launch": {"port": port, "query_port": query}, **extra})


def test_server_path_matches_the_manager(tmp_path, monkeypatch):
    """The SYSTEM helper builds the game exe path itself; it must agree with the manager's layout."""
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VSM_SERVER_DIR", raising=False)
    from vsm.core import paths

    monkeypatch.setattr(paths, "IS_WINDOWS", True)
    assert paths.Paths().server_exe == tmp_path.resolve().joinpath(*firewall.SERVER_EXE)


def test_read_ports(tmp_path):
    assert firewall.read_ports(config(tmp_path, 7779, 27016)) == (7779, 27016)


@pytest.mark.parametrize("port", [True, "7777", 7777.0, None, 0, 80, 1023, 65536, -1, [7777]])
def test_read_ports_rejects_anything_but_an_unprivileged_integer(tmp_path, port):
    with pytest.raises(FirewallError):
        firewall.read_ports(config(tmp_path, port=port))
    with pytest.raises(FirewallError):
        firewall.read_ports(config(tmp_path, query=port))


@pytest.mark.parametrize("content", ["", "not json", "[1, 2]", '"text"', '{"launch": 5}', "{}"])
def test_read_ports_rejects_malformed_files(tmp_path, content):
    with pytest.raises(FirewallError):
        firewall.read_ports(write(tmp_path / "config.json", content))


def test_read_ports_rejects_missing_oversize_and_non_files(tmp_path):
    with pytest.raises(FirewallError):
        firewall.read_ports(tmp_path / "missing.json")
    (tmp_path / "dir.json").mkdir()
    with pytest.raises(FirewallError):
        firewall.read_ports(tmp_path / "dir.json")
    big = write(tmp_path / "big.json", json.dumps({"launch": {"port": 7777, "query_port": 27015}, "pad": "x" * (firewall.MAX_FILE_BYTES + 10)}))
    with pytest.raises(FirewallError):
        firewall.read_ports(big)


def test_read_ports_rejects_links(tmp_path):
    real = config(tmp_path)
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("cannot create symlinks here")
    with pytest.raises(FirewallError):
        firewall.read_ports(link)


def install(tmp_path, **settings):
    app = tmp_path / "app"
    data = tmp_path / "data"
    app.mkdir()
    data.mkdir()
    write(app / "desktop.json", {"data_dir": str(data), "port": 8810, **settings})
    return app, data


def test_desired_rules_game_only(tmp_path):
    app, data = install(tmp_path)
    config(data, 7779, 27016)
    assert firewall.desired_rules(app) == [
        Rule(firewall.GAME_RULE, "UDP", (7779, 27016), str(data.joinpath(*firewall.SERVER_EXE))),
        # The manager may hear the same two ports, for the port-forwarding check.
        Rule(firewall.CHECK_RULE, "UDP", (7779, 27016), str(app / "VeinServerManager.exe")),
    ]


def test_web_rule_only_with_remote_access(tmp_path):
    app, data = install(tmp_path, port=9000)
    config(data, network={"remote_access": "yes"})  # must be a real boolean
    assert len(firewall.desired_rules(app)) == 2
    config(data, network={"remote_access": True})
    web = firewall.desired_rules(app)[2]
    assert web == Rule(firewall.WEB_RULE, "TCP", (9000,), str(app / "VeinServerManager.exe"), "private,domain")


def test_install_settings_are_validated(tmp_path):
    app, data = install(tmp_path, data_dir="relative/path")
    config(data)
    with pytest.raises(FirewallError):
        firewall.desired_rules(app)
    write(app / "desktop.json", {"data_dir": str(data), "port": "8810"})
    with pytest.raises(FirewallError):
        firewall.desired_rules(app)


def test_netsh_arguments_are_exact():
    rule = Rule(firewall.GAME_RULE, "UDP", (7777, 27015), r"C:\VeinServerManager\server\Vein\Binaries\Win64\VeinServer-Win64-Test.exe")
    assert firewall.add_args(rule) == [
        "advfirewall", "firewall", "add", "rule",
        "name=Vein Server Manager (game)", "dir=in", "action=allow", "enable=yes",
        "protocol=UDP", "localport=7777,27015",
        r"program=C:\VeinServerManager\server\Vein\Binaries\Win64\VeinServer-Win64-Test.exe",
        "profile=any",
    ]
    assert firewall.delete_args(firewall.GAME_RULE) == ["advfirewall", "firewall", "delete", "rule", "name=Vein Server Manager (game)"]


def test_sync_leaves_the_firewall_alone_when_settings_are_bad(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(firewall, "_netsh", lambda args: calls.append(args) or 0)
    app, data = install(tmp_path)
    config(data, port=22)
    assert firewall.sync(app) == 2 and calls == []
    config(data)
    assert firewall.sync(app) == 0
    assert [c[2] for c in calls] == ["delete", "delete", "delete", "delete", "add", "add"]
    assert firewall.sync(tmp_path / "nowhere") == 2


def test_parse_and_compare_rule():
    wanted = Rule(firewall.GAME_RULE, "UDP", (7777, 27015), r"C:\Data\server\Vein\Binaries\Win64\VeinServer-Win64-Test.exe")
    raw = {"enabled": True, "direction": "Inbound", "action": "Allow", "protocol": "UDP", "ports": ["27015", "7777"], "program": wanted.program.upper()}
    assert firewall.in_sync(firewall.parse_rule(json.dumps(raw)), wanted)
    assert firewall.in_sync(firewall.parse_rule(json.dumps({**raw, "ports": ["7777,27015"]})), wanted)
    assert not firewall.in_sync(firewall.parse_rule(json.dumps({**raw, "ports": ["7779", "27015"]})), wanted)
    assert not firewall.in_sync(firewall.parse_rule(json.dumps({**raw, "program": "Any"})), wanted)
    assert firewall.parse_rule(json.dumps({**raw, "enabled": False})) is None
    assert firewall.parse_rule(json.dumps({**raw, "action": "Block"})) is None
    assert firewall.parse_rule("null") is None and firewall.parse_rule("garbage") is None
    assert not firewall.in_sync(None, wanted)
