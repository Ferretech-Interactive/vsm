"""Desktop entry point: `python -m vsm.desktop`, or VeinServerManager.exe when packaged.

With no arguments it opens the window. What that window talks to depends on the mode in
desktop.json (see settings.py): the Windows service, or a manager hosted inside this process.
The other flags are helpers run by Setup, the service manager or the firewall task.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
import urllib.request
from http.cookiejar import CookieJar
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import settings


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=2) as r:
            return r.status == 200
    except OSError:
        return False


def _wait_healthy(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if _healthy(url):
            return True
        time.sleep(0.3)
    return False


def _setup_window_logging() -> None:
    """The window process has no console; keep its own log (including pywebview's)."""
    logs = settings.client_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(logs / "desktop.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for name in ("vsm.desktop", "vsm.localauth", "pywebview"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.DEBUG)
        logger.addHandler(handler)


def _setup_step(what: str, action) -> bool:
    """Run one elevated installer step. Failures go beside the exe (admin-only), never the data folder."""
    try:
        action()
        return True
    except Exception as e:
        try:
            with open(settings.APP_DIR / "setup-errors.log", "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} Could not {what}: {e}\n")
        except OSError:
            pass
        return False


def _require(ok: bool, problem: str) -> None:
    if not ok:
        raise RuntimeError(problem)


def _listener_pid(port: int) -> int | None:
    import psutil

    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr and conn.laddr.port == port:
                return conn.pid
    except (psutil.Error, OSError):
        pass
    return None


class _AdminApi:
    """Talks to the running service as an administrator (used by Setup's elevated helpers)."""

    def __init__(self, url: str):
        from . import localauth, system

        self.url = url
        if not _wait_healthy(url, 90):
            raise RuntimeError("the service did not start")
        pid = system.service_state()[1]
        if _listener_pid(settings.port()) != pid:
            raise RuntimeError(f"another program is using port {settings.port()}")
        code = localauth.request_code(expected_server_pid=pid)
        if not code:
            raise RuntimeError("the service refused the installer's sign-in")
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self.call("POST", "/api/auth/local", {"token": code})

    def call(self, method: str, route: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            self.url + route, data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json"}, method=method,
        )
        with self._opener.open(request, timeout=20) as response:
            return json.loads(response.read().decode())


def _apply_setup(path: Path, allow_remote: bool, url: str) -> None:
    """Apply Setup's choices through the service's own API, never by writing its files.

    The password arrives in a file (not on the command line, where other processes and the
    install log could see it) and the file is deleted as soon as it has been read.
    """
    from . import system

    password = path.read_text(encoding="utf-8-sig").strip("\r\n")
    path.unlink()
    if not password and not allow_remote:
        return
    api = _AdminApi(url)
    if password:
        api.call("POST", "/api/auth/password", {"new": password})
    if allow_remote and not api.call("GET", "/api/config")["network"]["remote_access"]:
        api.call("PUT", "/api/config", {"network": {"remote_access": True}})
        # The manager binds its address at start, so restart it to begin listening for other devices.
        system.stop_service()
        _require(system.start_service(), "Windows refused to restart the service")
        _require(_wait_healthy(url, 90), "the service did not come back")
        system.run_firewall_task()


def _stop_game_server(url: str) -> None:
    """Before uninstalling: stop the game through the manager so it can finish saving. Best effort."""
    try:
        if not _healthy(url):
            return
        api = _AdminApi(url)
        if api.call("GET", "/api/status")["state"] == "OFFLINE":
            return
        api.call("POST", "/api/server/stop", {})
        deadline = time.time() + 120
        while time.time() < deadline and api.call("GET", "/api/status")["state"] != "OFFLINE":
            time.sleep(2)
    except Exception:
        pass


def _service_window_target(url: str):
    """In service mode: make sure the service is up and really is what answers on the port.

    Returns the token provider for automatic sign-in.
    """
    from . import localauth, system

    state, pid = system.service_state()
    if state == "missing":
        system.message_box("The Vein Server Manager service is not installed. Run Setup again to repair it.")
        raise SystemExit(1)
    if state != "running":
        system.start_service()
    if not _wait_healthy(url, 60):
        system.message_box("The Vein Server Manager service is not running and could not be started.\n\nStart it from the Services app, or run Setup again to repair it.")
        raise SystemExit(1)
    state, pid = system.service_state()
    if _listener_pid(settings.port()) != pid:
        # Something else took the port while the service was down; never send it a sign-in code.
        system.message_box(f"Another program is using port {settings.port()}, so the manager cannot be reached safely.")
        raise SystemExit(1)
    def code() -> str | None:
        # Checked again for every code: the port could have changed hands since the window opened.
        pid = system.service_state()[1]
        return localauth.request_code(expected_server_pid=pid) if pid and _listener_pid(settings.port()) == pid else None

    return code


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="VeinServerManager", description="Vein Server Manager desktop app")
    parser.add_argument("--headless", action="store_true", help="run the manager with no window or tray icon (portable installs)")
    parser.add_argument("--service", action="store_true", help="run as the Windows service (started by Windows, not by hand)")
    parser.add_argument("--sync-firewall", action="store_true", help="make the firewall rules match the settings (run as SYSTEM by a task)")
    parser.add_argument("--install-service", action="store_true", help="create or update the service (admin; used by Setup)")
    parser.add_argument("--uninstall-service", action="store_true", help="remove the service, its task and firewall rules (admin)")
    parser.add_argument("--secure-program-folder", action="store_true", help="limit who may change the program folder (admin; used by Setup)")
    parser.add_argument("--apply-setup", metavar="FILE", help="apply Setup's choices; FILE holds the admin password and is deleted (admin)")
    parser.add_argument("--allow-remote", action="store_true", help="with --apply-setup: let other devices reach the manager")
    parser.add_argument("--register-startup", action="store_true", help="portable: start the manager when Windows starts (admin)")
    parser.add_argument("--unregister-startup", action="store_true", help="portable: remove the startup task (admin)")
    args = parser.parse_args(argv)

    if os.name != "nt":
        raise SystemExit("The desktop app is Windows-only. On Linux run `python -m vsm` and use a browser.")

    if args.sync_firewall:
        # Runs as SYSTEM. Keep it away from everything else in the manager (see firewall.py).
        from . import firewall

        raise SystemExit(firewall.sync(settings.APP_DIR))

    settings.apply(as_service=args.service)
    url = f"http://127.0.0.1:{settings.port()}"

    def build():
        from ..__main__ import build_server

        return build_server()

    if args.service:
        from .service import run_service

        run_service(build, lambda: _healthy(url))  # enters the service dispatcher before the heavy imports
        return

    from . import system

    if (args.install_service or args.uninstall_service or args.apply_setup or args.register_startup or args.unregister_startup
            or args.secure_program_folder):
        steps = [
            (args.secure_program_folder, "secure the program folder", lambda: system.secure_program_folder(str(Path(sys.executable).parent))),
            (args.unregister_startup, "remove the startup task", system.unregister_startup),
            (args.uninstall_service, "stop the game server", lambda: _stop_game_server(url)),
            (args.uninstall_service, "remove the service", system.uninstall_service),
            (args.install_service, "install the service", lambda: system.install_service(sys.executable, str(settings.data_dir()))),
            (args.install_service, "start the service", lambda: _require(system.start_service(), "Windows refused to start it")),
            (args.install_service, "set up the firewall rules", lambda: _require(_wait_healthy(url, 90) and system.run_firewall_task(), "the firewall task could not be run")),
            (bool(args.apply_setup), "apply the password and remote-access choice", lambda: _apply_setup(Path(args.apply_setup), args.allow_remote, url)),
            (args.register_startup, "register the startup task", system.register_startup),
        ]
        results = [_setup_step(what, action) for wanted, what, action in steps if wanted]
        raise SystemExit(0 if all(results) else 1)

    if args.headless:
        build().run()
        return

    instance = system.SingleInstance()
    if instance.already_running:
        instance.signal_existing()
        return
    _setup_window_logging()

    mode = settings.mode()
    server = thread = None
    if mode == "client":
        url, token_provider = None, None  # nothing runs on this PC; the window only opens other servers
    elif mode == "service":
        token_provider = _service_window_target(url)
    else:
        if not _healthy(url):
            try:
                server = build()
            except SystemExit as e:  # invalid config and the like; there is no console to print to
                system.message_box(str(e))
                raise
            thread = threading.Thread(target=server.run, name="manager", daemon=True)
            thread.start()
            if not _wait_healthy(url, 30):
                system.message_box(f"The manager could not start on port {settings.port()}. Is another program using it?")
                raise SystemExit(1)
        from ..api.auth import LOCAL_TOKEN

        token_file = settings.data_dir().joinpath(*LOCAL_TOKEN)
        port = settings.port()

        def token_provider() -> str | None:
            # Only while this process (the manager it hosts) is what answers on the port.
            if _listener_pid(port) != os.getpid() or not token_file.exists():
                return None
            return token_file.read_text(encoding="ascii").strip()

    from .shell import Shell

    try:
        Shell(url, instance, token_provider).run()
    except Exception:
        logging.getLogger("vsm.desktop").exception("the window failed")
        system.message_box("The window could not be opened. Details are in logs\\desktop.log.")
        raise
    finally:
        if server:
            server.should_exit = True  # runs the manager's normal shutdown
            thread.join(timeout=200)


if __name__ == "__main__":
    sys.exit(main())
