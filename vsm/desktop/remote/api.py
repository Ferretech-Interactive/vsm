"""The Deploy window's bridge. Like the Servers and About windows, it belongs to that window
only, which shows local HTML and never a page from a server.

Every method answers with a dict; a failure is `{"error": "..."}` in words meant for the person.
"""
from __future__ import annotations

import logging

from ... import __version__
from . import deploy
from .ssh import SshError

log = logging.getLogger("vsm.desktop.remote")


class DeployApi:
    def __init__(self, shell):
        self._shell = shell
        self._job: deploy.Deployment | None = None
        self._name = ""
        self._password: str | None = None  # of a new install: shown once, used once to sign the window in
        self._server_id: str | None = None

    def _try(self, action) -> dict:
        try:
            return action() or {}
        except SshError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001
            log.exception("deploy window action failed")
            return {"error": f"Something went wrong: {type(e).__name__}. Details are in the desktop log."}

    def info(self) -> dict:
        return {"available": deploy.available(), "version": __version__}

    def probe(self, host, port, user) -> dict:
        def action():
            if self._job and self._job.state == "working":
                raise SshError("An installation is still running.")
            self._job = deploy.Deployment(self._shell.servers.known_hosts(), self._shell.tunnel_key_for)
            return self._job.probe(host, port, user)

        return self._try(action)

    def browse_key(self) -> dict:
        return {"path": self._shell.pick_file(self)}

    def check(self, auth) -> dict:
        return self._try(lambda: self._job.check(auth if isinstance(auth, dict) else {}) if self._job else {"error": "Start again from the first step."})

    def install(self, kind, name) -> dict:
        def action():
            if not self._job:
                raise SshError("Start again from the first step.")
            self._name = str(name or "").strip()[:60] or self._job.target.host
            self._job.install(str(kind), self._saved)

        return self._try(action)

    def _saved(self, result: dict, private: bytes) -> None:
        self._server_id = self._shell.save_deployed(self._name, result, private)

    def status(self) -> dict:
        if not self._job:
            return {"state": "idle", "step": "", "lines": [], "error": None, "result": None}
        status = self._job.status()
        if status["state"] == "done" and self._password is None:
            self._password = self._job.take_password()
        return dict(status, password=self._password)

    def open(self) -> dict:
        """Show the deployed server in the main window, signing in once if it is a new install."""
        if self._server_id:
            self._shell.connect(self._server_id, login=self._password)
        self.forget()
        self._shell.close_deploy()
        return {}

    def close_window(self) -> dict:
        self.forget()
        self._shell.close_deploy()
        return {}

    def forget(self) -> None:
        """Drop everything typed or generated here. An installation still running keeps its connection."""
        self._password = None
        if self._job and self._job.state != "working":
            self._job.close()
            self._job = None
