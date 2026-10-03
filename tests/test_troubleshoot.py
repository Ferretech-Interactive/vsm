from vsm.server.troubleshoot import EOS_SPAM, check, scan

# Lines as the dedicated server writes them (build 25708939), plus the wording Ramjet's page quotes.
STARTING = [
    "[2026.10.07-00.41.02:264][  0]LogRamjetNetworking: Steamworks server initialized with SteamID 90071992547409920.",
    "[2026.10.07-00.41.03:380][  0]LogRamjetNetworking: Warning: Failed to heartbeat (no connection string).",
    "[2026.10.07-00.41.08:533][260]LogRamjetNetworking: Warning: Failed to heartbeat (no connection string).",
]
CONNECTED = [
    "[2026.10.07-00.41.17:886][796]LogRamjetNetworking: S_OnSteamServersConnected",
    "[2026.10.07-00.41.18:456][830]LogRamjetNetworking: Heartbeating with IP 108.28.158.174:9999.",
]


def ids(findings):
    return {f["id"]: f["level"] for f in findings}


def test_a_healthy_start_reports_the_address_it_registered():
    found = scan(STARTING + CONNECTED, windows=True, running=True)
    assert ids(found) == {"clean": "ok", "listed": "ok"}
    assert found[1]["detail"] == "The server is announcing itself as 108.28.158.174:9999."


def test_heartbeat_failures_only_matter_while_they_continue():
    assert ids(scan(STARTING, windows=True, running=True)) == {"heartbeat": "warn"}
    assert ids(scan(STARTING, windows=True, running=False)) == {"clean": "ok"}  # an old log from a stopped server
    # Failing again after it had connected is a problem again; the documented wording counts too.
    later = STARTING + CONNECTED + ["LogRamjetNetworking: Failed to get connection string for heartbeat: no IP & not running Steam Sockets P2P"]
    assert ids(scan(later, windows=False, running=True)) == {"heartbeat": "warn"}


def test_steam_problems():
    library = ["SteamAPI_Init(): Sys_LoadModule failed to load: C:\\server\\steamclient64.dll"]
    found = scan(library, windows=True, running=True)
    assert ids(found) == {"steam_library": "error"} and "steam_api64.dll" in found[0]["detail"]
    found = scan(["dlopen failed trying to load: /home/vein/.steam/sdk64/steamclient.so"], windows=False, running=True)
    assert ids(found) == {"steam_library": "error"} and "steamclient.so" in found[0]["detail"]
    assert ids(scan(["LogSteam: Error: Steam Dedicated Server API failed to initialize."], windows=True, running=True)) == {"steam_init": "error"}


def test_eos_spam_is_only_a_note():
    spam = ["LogOnlineSession: Warning: EOS: session update skipped"] * EOS_SPAM
    assert ids(scan(spam[:-1], windows=True, running=True)) == {"clean": "ok"}
    found = scan(spam, windows=True, running=True)
    assert ids(found) == {"clean": "ok", "eos_spam": "info"} and "[Core.Log]" in found[1]["detail"]


def test_check_reads_the_end_of_the_log(tmp_path):
    assert check(tmp_path / "Vein.log", windows=True, running=False) == {"log_found": False, "findings": []}
    (tmp_path / "Vein.log").write_text("\n".join(STARTING + CONNECTED), encoding="utf-8")
    assert ids(check(tmp_path / "Vein.log", windows=True, running=True)["findings"]) == {"clean": "ok", "listed": "ok"}
