"""Runs the manager as a Windows service.

`ServiceRunner` is plain Python so it can be tested anywhere; `run_service()` is the thin
pywin32 layer that connects it to the Service Control Manager.

Stop behaviour (chosen by the project owner):
  * the service is stopped, or the manager is being upgraded -> the game server keeps
    running and is adopted again when the service returns;
  * Windows is shutting down -> the game server is stopped properly, waiting for a save.
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("vsm.service")

SERVICE_NAME = "VeinServerManager"
DISPLAY_NAME = "Vein Server Manager"
DESCRIPTION = "Runs the Vein dedicated game server and its manager."
ACCOUNT = "NT SERVICE\\" + SERVICE_NAME
PRESHUTDOWN_MS = 180_000  # how long Windows waits for us at shutdown


class ServiceRunner:
    """Owns the manager's HTTP server thread on behalf of the service."""

    def __init__(self, build_server, is_healthy, start_timeout: float = 60):
        self._build_server = build_server
        self._is_healthy = is_healthy
        self._start_timeout = start_timeout
        self.server = None
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the manager and wait until it answers. Raises RuntimeError with the reason if it cannot."""
        try:
            self.server = self._build_server()
        except SystemExit as e:  # raised for an invalid config file or VSM_* setting
            raise RuntimeError(str(e) or "the manager's configuration is invalid")
        # The manager can ask for a restart (to rebind after a network setting changes): it just
        # stops serving, and the service reports that as a failure so Windows starts it again.
        self.server.config.app.state.manager.restart_hook = lambda: setattr(self.server, "should_exit", True)
        self.thread = threading.Thread(target=self._run, name="manager", daemon=True)
        self.thread.start()
        deadline = time.time() + self._start_timeout
        while time.time() < deadline:
            if not self.thread.is_alive():
                raise RuntimeError("the manager stopped while starting (is the port already in use?)")
            if self._is_healthy():
                return
            time.sleep(0.25)
        raise RuntimeError("the manager did not start listening in time")

    def _run(self) -> None:
        try:
            self.server.run()
        except SystemExit:  # uvicorn exits this way when it cannot bind the port
            pass
        except Exception:
            log.exception("the manager crashed")

    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def stop(self, stop_game: bool, timeout: float = 170) -> None:
        if not self.server:
            return
        self.server.config.app.state.manager.stop_game_on_exit = stop_game
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout)


def run_service(build_server, is_healthy) -> None:
    """Hand control to the Service Control Manager. Returns when the service has stopped."""
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
    import winerror

    class Service(win32serviceutil.ServiceFramework):
        _svc_name_ = SERVICE_NAME
        _svc_display_name_ = DISPLAY_NAME

        def __init__(self, args):
            super().__init__(args)
            self.stop_requested = win32event.CreateEvent(None, 0, 0, None)
            self.stop_game = False

        def GetAcceptedControls(self):
            # PRESHUTDOWN instead of SHUTDOWN: it is the only shutdown notice that gives a
            # service more than a few seconds, and the game needs time to save.
            return win32service.SERVICE_ACCEPT_STOP | win32service.SERVICE_ACCEPT_PRESHUTDOWN

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30_000)
            win32event.SetEvent(self.stop_requested)

        def SvcOtherEx(self, control, event_type, data):
            if control == win32service.SERVICE_CONTROL_PRESHUTDOWN:
                self.stop_game = True
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=30_000)
                win32event.SetEvent(self.stop_requested)

        def SvcDoRun(self):
            runner = ServiceRunner(build_server, is_healthy)
            try:
                runner.start()
            except RuntimeError as e:
                log.error("service could not start: %s", e)
                self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=winerror.ERROR_SERVICE_SPECIFIC_ERROR, svcExitCode=1)
                return
            crashed = False
            while win32event.WaitForSingleObject(self.stop_requested, 2000) != win32event.WAIT_OBJECT_0:
                if not runner.alive():
                    crashed = True
                    break
            stopper = threading.Thread(target=runner.stop, args=(self.stop_game,), daemon=True)
            stopper.start()
            while stopper.is_alive():  # keep telling Windows we are still working on it
                self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=15_000)
                stopper.join(3)
            if crashed:  # a non-zero exit makes Windows apply the restart-on-failure setting
                self.ReportServiceStatus(win32service.SERVICE_STOPPED, win32ExitCode=winerror.ERROR_SERVICE_SPECIFIC_ERROR, svcExitCode=2)

    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(Service)
    servicemanager.StartServiceCtrlDispatcher()
