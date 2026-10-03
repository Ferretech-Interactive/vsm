import sys
import time
import types

import pytest

from vsm.desktop.localauth import (
    ADMINISTRATORS, MEDIUM_INTEGRITY, SE_GROUP_ENABLED, SE_GROUP_USE_FOR_DENY_ONLY, is_local_admin,
)
from vsm.desktop.service import ServiceRunner

USERS = "S-1-5-32-545"
HIGH, LOW = 0x3000, 0x1000


# ---------- who counts as a local administrator ----------

@pytest.mark.parametrize("groups, integrity, app_container, restricted, expected", [
    ([(USERS, SE_GROUP_ENABLED), (ADMINISTRATORS, SE_GROUP_ENABLED)], HIGH, False, False, True),  # elevated admin
    ([(USERS, SE_GROUP_ENABLED), (ADMINISTRATORS, SE_GROUP_USE_FOR_DENY_ONLY)], MEDIUM_INTEGRITY, False, False, True),  # admin, not elevated (UAC)
    ([(USERS, SE_GROUP_ENABLED)], MEDIUM_INTEGRITY, False, False, False),  # standard user
    ([(ADMINISTRATORS, 0)], MEDIUM_INTEGRITY, False, False, False),  # group listed but neither enabled nor deny-only
    ([(ADMINISTRATORS, SE_GROUP_USE_FOR_DENY_ONLY)], LOW, False, False, False),  # sandboxed process of an admin
    ([(ADMINISTRATORS, SE_GROUP_USE_FOR_DENY_ONLY)], MEDIUM_INTEGRITY, True, False, False),  # AppContainer (store app)
    ([(ADMINISTRATORS, SE_GROUP_ENABLED)], HIGH, False, True, False),  # restricted token
    ([], HIGH, False, False, False),
])
def test_is_local_admin(groups, integrity, app_container, restricted, expected):
    assert is_local_admin(groups, integrity, app_container, restricted) is expected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")
def test_pipe_gives_this_user_a_code_or_a_refusal():
    """Real pipe, real token inspection, as whoever runs the tests (normally a non-elevated admin)."""
    pytest.importorskip("win32pipe")
    import os

    import win32api
    import win32con
    import win32security

    from vsm.desktop import localauth

    name = rf"\\.\pipe\vsm-test-{os.getpid()}"
    server = localauth.PipeServer(lambda: "one-time-code", name)
    server.start()
    try:
        reply = None
        for _ in range(50):
            reply = localauth.request_code(name, expected_server_pid=os.getpid())
            if reply is not None or _pipe_exists(name):
                break
            time.sleep(0.1)
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        expected = "one-time-code" if is_local_admin(*localauth._describe_token(token)) else None
        assert reply == expected
        # a second connection works (the server keeps listening), and the wrong owner is ignored
        assert localauth.request_code(name, expected_server_pid=os.getpid()) == expected
        assert localauth.request_code(name, expected_server_pid=4) is None
        assert localauth.request_code(name + "-nobody-home") is None
    finally:
        server.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows named pipes")
def test_pipe_is_ready_for_the_next_caller_when_the_last_one_hung_up_early(monkeypatch):
    """A caller that reads its code and closes before the server's last read used to leave the
    server waiting two seconds for an operation that never started, turning the next caller away."""
    pytest.importorskip("win32pipe")
    import os

    from vsm.desktop import localauth

    plain_wait = localauth.PipeServer._wait

    def late(self, overlapped, milliseconds):  # a server thread that is a little behind, as on a busy PC
        done = plain_wait(self, overlapped, milliseconds)
        if milliseconds is not None:
            time.sleep(0.05)
        return done

    monkeypatch.setattr(localauth.PipeServer, "_wait", late)
    name = rf"\\.\pipe\vsm-test-early-{os.getpid()}"
    server = localauth.PipeServer(lambda: "one-time-code", name)
    server.start()
    try:
        for _ in range(50):
            if _pipe_exists(name):
                break
            time.sleep(0.1)
        started = time.monotonic()
        replies = [localauth.request_code(name, expected_server_pid=os.getpid()) for _ in range(4)]
        assert len(set(replies)) == 1  # every caller got the same answer as the first: nobody was turned away
        assert time.monotonic() - started < 3
    finally:
        server.stop()


def _pipe_exists(name: str) -> bool:
    import win32pipe

    try:
        win32pipe.WaitNamedPipe(name, 100)
        return True
    except Exception:
        return False


# ---------- the service's handling of the manager thread ----------

class FakeServer:
    def __init__(self, die_after=None):
        self.should_exit = False
        self.manager = types.SimpleNamespace(stop_game_on_exit=None)
        self.config = types.SimpleNamespace(app=types.SimpleNamespace(state=types.SimpleNamespace(manager=self.manager)))
        self._die_after = die_after

    def run(self):
        started = time.time()
        while not self.should_exit:
            if self._die_after is not None and time.time() - started > self._die_after:
                raise SystemExit(1)  # what uvicorn does when the port is taken
            time.sleep(0.01)


def test_runner_starts_and_stops_with_the_right_stop_mode():
    for stop_game in (False, True):
        server = FakeServer()
        runner = ServiceRunner(lambda: server, lambda: True)
        runner.start()
        assert runner.alive()
        runner.stop(stop_game)
        assert not runner.alive() and server.manager.stop_game_on_exit is stop_game


def test_runner_reports_bad_config():
    def build():
        raise SystemExit("config.json is invalid")

    with pytest.raises(RuntimeError, match="config.json is invalid"):
        ServiceRunner(build, lambda: True).start()


def test_runner_reports_early_death_and_timeout():
    with pytest.raises(RuntimeError, match="stopped while starting"):
        ServiceRunner(lambda: FakeServer(die_after=0.05), lambda: False, start_timeout=5).start()
    runner = ServiceRunner(lambda: FakeServer(), lambda: False, start_timeout=0.3)
    with pytest.raises(RuntimeError, match="did not start listening"):
        runner.start()
    runner.stop(False)


def test_runner_notices_a_crash_after_start():
    runner = ServiceRunner(lambda: FakeServer(die_after=0.1), lambda: True)
    runner.start()
    time.sleep(0.4)
    assert not runner.alive()


# ---------- installer helpers ----------

@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only module")
def test_service_sid_and_start_right():
    from vsm.desktop import system

    # Known value: the SID of NT SERVICE\TrustedInstaller on every Windows machine.
    assert system.service_sid("TrustedInstaller") == "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"
    sddl = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCLCSWLOCRRC;;;IU)S:(AU;FA;CC;;;WD)"
    granted = system.add_start_right(sddl)
    assert granted == "D:(A;;RP;;;IU)" + sddl[2:]
    assert system.add_start_right(granted) == granted  # applying it twice changes nothing


# ---------- stopping the manager does not relaunch the game ----------

def test_shutdown_blocks_a_pending_start(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "correct-horse-battery")
    from vsm.manager import Manager

    m = Manager()
    events = []
    monkeypatch.setattr(m, "event", lambda category, message: events.append(message))
    m.server.shutdown(stop_game=False)
    m.server._do_start("RESTART")  # what an in-flight restart or the watchdog would call next
    assert events == [] and m.server.pid is None


def test_shutdown_stop_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("VSM_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("VSM_ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.delenv("VSM_STOP_SERVER_ON_EXIT", raising=False)
    from vsm.manager import Manager

    for stop_game, expected in ((False, []), (True, ["stop"]), (None, [])):
        m = Manager()
        m.server.pid = 4242  # pretend a game server is running
        calls = []
        monkeypatch.setattr(m.server, "_do_stop", lambda trigger, calls=calls: calls.append("stop"))
        m.stop_game_on_exit = stop_game
        m.shutdown()
        assert calls == expected
