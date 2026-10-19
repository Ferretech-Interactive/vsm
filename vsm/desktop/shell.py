"""The window and tray icon. Both are thin: all behaviour lives in the web UI and the manager's API."""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.cookiejar import CookieJar
from pathlib import Path

import pystray
import webview
from PIL import Image

from urllib.parse import urlsplit, urlunsplit

from .. import __version__
from . import settings, system, theme, updater
from .about_page import LINKS, about_html
from .deploy_page import DEPLOY_HTML
from .servers import LOCAL_ID, ServerError, ServerList, is_unencrypted_public, tunnel_url
from .servers_page import PLACEHOLDER_HTML, SERVERS_HTML

log = logging.getLogger("vsm.desktop")

ICON = Path(__file__).parent / "icon.png"
TITLE = "Vein Server Manager"

# Runs inside the window on every page load. The token never appears in a URL. The page is checked
# again inside the script: the window may have moved on between deciding to sign in and the script running.
SIGN_IN_JS = """
(async () => {
  if (location.origin !== %(origin)s) return;
  const state = await (await fetch('/api/auth/state')).json();
  if (state.authenticated) return;
  const r = await fetch('/api/auth/local', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({token: %(secret)s})
  });
  if (r.ok) location.reload();
})();
"""


# Signs the window in to a server this app has just deployed, with the password it generated.
# Used once, and only on that server's own page (see _on_loaded).
LOGIN_JS = """
(async () => {
  if (location.origin !== %(origin)s) return;
  const state = await (await fetch('/api/auth/state')).json();
  if (state.authenticated) return;
  const r = await fetch('/api/auth/login', {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({password: %(secret)s})
  });
  if (r.ok) location.reload();
})();
"""

CONNECTING_HTML = PLACEHOLDER_HTML.replace("No server selected", "Connecting through SSH...").replace(
    "Choose Servers &gt; Manage servers to add one.", "This takes a moment.")

WEBVIEW2_ENV = "WEBVIEW2_BROWSER_EXECUTABLE_FOLDER"
WEBVIEW2_CLIENT = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
WEBVIEW2_DOWNLOAD = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"


def use_working_webview2() -> None:
    """Point WebView2 at a runtime that is really on disk when the registered one is not.

    Windows can list a WebView2 version whose folder no longer exists (seen in Windows
    Sandbox, and after interrupted Edge updates) while a newer version sits beside it.
    WebView2 then refuses to start and the window stays blank.
    """
    if os.environ.get(WEBVIEW2_ENV):
        return
    import winreg

    for hive, prefix in ((winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\"), (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\"), (winreg.HKEY_CURRENT_USER, "Software\\")):
        try:
            with winreg.OpenKey(hive, prefix + WEBVIEW2_CLIENT) as key:
                version = winreg.QueryValueEx(key, "pv")[0]
                location = winreg.QueryValueEx(key, "location")[0]
        except OSError:
            continue
        root = Path(location)
        if (root / version / "msedgewebview2.exe").is_file():
            return  # the registered runtime is fine
        present = [p.parent for p in root.glob("*/msedgewebview2.exe")]
        if present:
            newest = max(present, key=lambda p: tuple(int(n) if n.isdigit() else 0 for n in p.name.split(".")))
            os.environ[WEBVIEW2_ENV] = str(newest)
            log.warning("WebView2 %s is registered but missing; using %s instead", version, newest)
            return


class _WebViewFailure(logging.Handler):
    """pywebview only logs a WebView2 start-up failure; this turns that log line into a callback."""

    def __init__(self, callback):
        super().__init__(logging.ERROR)
        self._callback = callback
        self._fired = False

    def emit(self, record: logging.LogRecord) -> None:
        if not self._fired and "WebView2 initialization failed" in record.getMessage():
            self._fired = True
            self._callback()


def window_url(url: str) -> str:
    """The address the window opens for a manager: marked, so the page knows it is inside the app
    (which has its own Help > About) and not in a browser. The window has no bridge to say so."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", "desktop=1", ""))


def origin_of(url: str) -> str:
    """`location.origin` of a page at `url`, as the browser writes it (default ports left out)."""
    parts = urlsplit(url)
    default = {"http": 80, "https": 443}.get(parts.scheme)
    port = f":{parts.port}" if parts.port and parts.port != default else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def expose_api(window, api) -> None:
    """Give a window's page the public methods of `api`, and nothing else.

    Not passed as pywebview's js_api: pywebview looks a call's name up attribute by attribute
    along its dots, so a page could reach `_shell` (and from there anything) through the object.
    Exposed functions are looked up by exact name only. Each is a plain function with the
    method's parameter names, so the page calls them as before (pywebview.api.name(...))."""
    import inspect

    for name in dir(type(api)):
        if name.startswith("_") or not callable(getattr(type(api), name)):
            continue
        method = getattr(api, name)
        params = list(inspect.signature(method).parameters)
        source = f"def {name}({', '.join(params)}):\n    return _method({', '.join(params)})\n"
        scope = {"_method": method}
        exec(source, scope)  # noqa: S102 - names come from our own classes
        window.expose(scope[name])


class _SafeBrowser:
    """What pywebview uses to open a page's new-window links: web addresses only. Handing anything
    else to Windows (search-ms:, file:, ms-...:) would let a page from a remote manager start programs."""

    @staticmethod
    def open(url, *args, **kwargs) -> bool:
        if urlsplit(str(url)).scheme.lower() in ("http", "https"):
            return webbrowser.open(str(url))
        log.warning("refused to open a %s: link from a page", urlsplit(str(url)).scheme or "schemeless")
        return False


def install_safe_browser() -> None:
    from webview.platforms import edgechromium

    if not hasattr(edgechromium, "webbrowser"):  # pywebview changed: fail loudly rather than silently unprotected
        raise RuntimeError("pywebview no longer opens links the expected way; review _SafeBrowser")
    edgechromium.webbrowser = _SafeBrowser()


def is_local_origin(page_url: str, local_url: str | None) -> bool:
    """Is `page_url` a page of this PC's own manager? Scheme, host and port must all match exactly.

    The local sign-in credential is only ever offered to such a page: any other origin would
    simply pass it on to its own server.
    """
    if not local_url or not page_url:
        return False
    try:
        page, local = urlsplit(page_url), urlsplit(local_url)
        return (page.scheme, page.hostname, page.port) == (local.scheme, local.hostname, local.port) and bool(page.hostname)
    except ValueError:
        return False


class ServersApi:
    """The Servers window's bridge. Deliberately tiny: list, add, remove, connect."""

    def __init__(self, shell: "Shell"):
        self._shell = shell

    def list(self) -> dict:
        return self._shell.servers_state()

    def add(self, name: str, address: str) -> dict:
        try:
            self._shell.servers.add(str(name), str(address))
        except ServerError as e:
            return self._shell.servers_state(str(e))
        return self._shell.servers_state()

    def deploy(self) -> dict:
        self._shell.open_deploy()
        return {}

    def remove(self, server_id: str) -> dict:
        try:
            self._shell.forget_server(str(server_id))
        except ServerError as e:
            return self._shell.servers_state(str(e))
        return self._shell.servers_state()

    def connect(self, server_id: str) -> dict:
        self._shell.connect(str(server_id))
        return self._shell.servers_state()


class AboutApi:
    """The About window's bridge: open one of a fixed set of links, check for updates, close."""

    def __init__(self, shell: "Shell"):
        self._shell = shell

    def open(self, name: str) -> None:
        url = LINKS.get(str(name))
        if url:
            webbrowser.open(url)

    def check_for_updates(self) -> None:
        if self._shell.can_update:
            self._shell.check_for_updates()

    def close(self) -> None:
        window = self._shell._about_window
        if window is not None:
            window.destroy()


class Shell:
    def __init__(self, url: str | None, single_instance=None, token_provider=None):
        """`url` is this PC's own manager, or None when the app is only a client for other machines."""
        self.url = url
        self.single_instance = single_instance
        self._token_provider = token_provider or (lambda: None)
        self.servers = ServerList(settings.client_dir() / "servers.json", url)
        self.current: dict | None = None
        self._servers_window = None
        self._about_window = None
        self._deploy_window = None
        self._tunnels: dict = {}  # server id -> the SSH tunnel to it (remote/tunnel.py)
        self._pending_login: tuple[str, str] | None = None  # (origin, password) of a server just deployed
        self.window = None
        self.tray = None
        self._quitting = False
        self._no_window = False
        self._state_file = settings.client_dir() / "desktop-window.json"
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        self._updating = threading.Lock()
        # One-click updates are for the installed program; the portable build is replaced by hand.
        self.can_update = settings.FROZEN and settings.mode() in ("service", "client")

    # ---------- manager API (for tray actions) ----------

    def _token(self) -> str | None:
        """A credential for this PC's own manager: the token file, or a one-time code from the service."""
        try:
            return self._token_provider()
        except Exception as e:
            log.warning("could not get a local sign-in credential: %s", e)
            return None

    def _is_local_page(self) -> bool:
        """True only while the window shows this PC's own manager.

        The local credential must never be offered to any other origin: a page from another
        host would simply forward it to its own server.
        """
        return is_local_origin(self.window.get_current_url() or "", self.url)

    def _api(self, path: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            f"{self.url}/api{path}",
            data=json.dumps(body or {}).encode() if body is not None else None,
            headers={"Content-Type": "application/json"},
            method="POST" if body is not None else "GET",
        )
        with self._opener.open(request, timeout=5) as response:
            return json.loads(response.read().decode())

    def _server_action(self, action: str) -> None:
        try:
            token = self._token()
            if not token:
                raise RuntimeError("not signed in")
            self._api("/auth/local", {"token": token})
            self._api(f"/server/{action}", {})
        except Exception as e:  # shown in the UI's event log anyway; the tray has nowhere to report
            log.warning("tray action %s failed: %s", action, e)

    # ---------- about ----------

    def about(self, *_args) -> None:
        if self._about_window is not None:
            try:
                self._about_window.show()
                self._about_window.restore()
                return
            except Exception:
                self._about_window = None
        kind = {"service": "Installed: runs as a Windows service", "client": "Installed: connects to other servers only"}.get(settings.mode(), "Portable")
        window = webview.create_window(
            "About Vein Server Manager", html=about_html(__version__, kind, ICON, self.can_update),
            width=440, height=530, resizable=False, background_color="#09090b",
        )
        expose_api(window, AboutApi(self))
        window.events.closed += lambda: setattr(self, "_about_window", None)
        theme.darken_when_shown(window)
        self._about_window = window

    # ---------- report a problem ----------

    def report_problem(self, *_args) -> None:
        """Help > Report a problem: show the System page's report card. On this PC's own manager,
        first hand it this window's desktop.log, which the service cannot read from this user's
        profile, so the diagnostics zip includes it."""
        threading.Thread(target=self._report_flow, name="report", daemon=True).start()

    def _report_flow(self) -> None:
        if self.window is None:
            return
        if self.url and self._is_local_page():
            try:
                from ..core.support import DESKTOP_LOG_MAX, tail

                token = self._token()
                if not token:
                    raise RuntimeError("not signed in")
                self._api("/auth/local", {"token": token})
                text = tail(settings.client_dir() / "logs" / "desktop.log", DESKTOP_LOG_MAX - 1000) or ""
                self._api("/support/desktop-log", {"text": text})
            except Exception as e:  # the zip is still useful without it
                log.warning("could not hand over the desktop log: %s", e)
        self.show()
        self.window.evaluate_js("location.hash = '/system'; setTimeout(() => window.dispatchEvent(new Event('vsm-report')), 300);")

    # ---------- updates ----------

    def check_for_updates(self, *_args) -> None:
        """Menu and tray entry, and what a page's "Update now" leads to. Asks before doing anything."""
        threading.Thread(target=self._update_flow, name="update", daemon=True).start()

    def _update_flow(self) -> None:
        if not self._updating.acquire(blocking=False):
            return  # already asking or downloading
        try:
            try:
                stable, beta = updater.pick_release(updater.fetch_releases(), __version__)
            except Exception as e:
                log.warning("update check failed: %s", e)
                system.message_box("Could not reach GitHub to check for updates. Check the internet connection and try again.", error=False)
                return
            if not stable:
                if beta:
                    if system.ask(f"{beta['tag']} is available as a pre-release.\n\nPre-releases are not signed, so they are installed by hand. Open its download page?"):
                        webbrowser.open(beta["page"])
                else:
                    system.message_box(f"You have the latest version ({__version__}).", error=False)
                return
            question = (
                f"Version {stable['tag']} is available (you have {__version__}).\n\n"
                "Download and install it now? Your settings are kept and a running game server keeps running. "
                "Windows will ask for permission, then the app reopens."
            )
            if not system.ask(question):
                return
            if self.tray:
                try:
                    self.tray.notify(f"Downloading {stable['tag']}...", TITLE)
                except Exception:
                    pass
            path = updater.download(stable, settings.client_dir() / "updates")
            updater.verify_and_run(path)
            log.info("started Setup for %s", stable["tag"])
            self.quit()  # Setup replaces this program's files and reopens the app
        except updater.UpdateError as e:
            system.message_box(f"The update was not installed: {e}.")
        except Exception:
            log.exception("the update failed")
            system.message_box("The update was not installed. Details are in the desktop log.")
        finally:
            self._updating.release()

    def _watch_update_requests(self) -> None:
        """The main window has no bridge into this process, so a page asks through the manager."""
        signed_in = False
        while not self._quitting:
            time.sleep(3)
            try:
                if not signed_in:
                    token = self._token()
                    if not token:
                        time.sleep(60)  # not an administrator: nothing to wait for
                        continue
                    self._api("/auth/local", {"token": token})
                    signed_in = True
                if self._api("/update/install-request").get("requested"):
                    self.check_for_updates()
            except urllib.error.HTTPError as e:
                signed_in = signed_in and e.code not in (401, 403)
            except Exception:
                signed_in = False  # the service is restarting or stopped; try again quietly
                time.sleep(10)

    # ---------- window ----------

    def _saved_size(self) -> tuple[int, int]:
        try:
            state = json.loads(self._state_file.read_text(encoding="utf-8"))
            return max(900, int(state["width"])), max(600, int(state["height"]))
        except (OSError, ValueError, KeyError):
            return 1280, 820

    def _on_loaded(self) -> None:
        log.info("page loaded (renderer: %s)", getattr(webview, "renderer", "unknown"))
        pending, self._pending_login = self._pending_login, None
        if pending and is_local_origin(self.window.get_current_url() or "", pending[0]):
            try:
                self.window.evaluate_js(LOGIN_JS % {"origin": json.dumps(origin_of(pending[0])), "secret": json.dumps(pending[1])})
            except Exception as e:
                log.warning("sign-in to the deployed server failed: %s", e)
            return
        if not self._is_local_page():
            return
        token = self._token()
        if not token:
            return  # not an administrator, or no local manager: the page shows its login form
        try:
            self.window.evaluate_js(SIGN_IN_JS % {"origin": json.dumps(origin_of(self.url)), "secret": json.dumps(token)})
        except Exception as e:
            log.warning("automatic sign-in failed: %s", e)

    def _on_closing(self) -> bool:
        if self._quitting:
            try:
                self._state_file.write_text(json.dumps({"width": self.window.width, "height": self.window.height}))
            except OSError:
                pass
            return True
        self.window.hide()  # keep running in the tray
        return False

    # ---------- other servers ----------

    def servers_state(self, error: str | None = None) -> dict:
        current = self.current["id"] if self.current else None
        return {
            "error": error,
            "servers": [
                {
                    "id": entry["id"], "name": entry["name"], "url": entry["url"],
                    "ssh": entry.get("kind") == "ssh",
                    "detail": f"through SSH to {entry['host']}" if entry.get("kind") == "ssh" else None,
                    "local": entry["id"] == LOCAL_ID,
                    "current": entry["id"] == current,
                    "warning": "Not encrypted: the password would cross the internet in clear text. Use https or a VPN."
                    if entry["id"] != LOCAL_ID and is_unencrypted_public(entry["url"]) else None,
                }
                for entry in self.servers.entries()
            ],
        }

    def connect(self, server_id: str, login: str | None = None) -> None:
        """Show a saved server in the main window. `login`: the password of a server this app has
        just deployed, to sign in with once."""
        entry = self.servers.get(server_id)
        if not entry:
            return
        if entry.get("kind") == "ssh":
            # Signing in over SSH takes a moment and must not freeze whichever window asked.
            threading.Thread(target=self._connect_ssh, args=(entry, login), name="ssh-connect", daemon=True).start()
            return
        self._close_tunnels()
        self._open(entry)

    def _connect_ssh(self, entry: dict, login: str | None) -> None:
        try:
            entry = self._tunnel(entry)
        except Exception as e:
            log.warning("could not open the SSH tunnel to %s: %s", entry["host"], e)
            system.message_box(f"Could not connect to {entry['name']} through SSH.\n\n{e}")
            return
        self._pending_login = (entry["url"], login) if login else None
        self._open(entry)

    def _tunnel(self, entry: dict) -> dict:
        """Make sure the tunnel to this deployed server is up; returns the entry with its address."""
        from .remote.tunnel import Tunnel

        self._close_tunnels(keep=entry["id"])
        tunnel = self._tunnels.get(entry["id"])
        if tunnel is None:
            tunnel = Tunnel(entry["host"], entry["ssh_port"], entry["host_key"], self._keys().load(entry["id"]),
                            entry["remote_port"], entry["local_port"])
            tunnel.start()
            self._tunnels[entry["id"]] = tunnel
            self.servers.set_local_port(entry["id"], tunnel.local_port)  # the saved one may have been taken
        entry = dict(entry, local_port=tunnel.local_port)
        return dict(entry, url=tunnel_url(entry))

    def _close_tunnels(self, keep: str | None = None) -> None:
        for server_id in [i for i in self._tunnels if i != keep]:
            self._tunnels.pop(server_id).stop()

    def _keys(self):
        from .remote.keys import KeyStore

        return KeyStore(settings.client_dir() / "keys")

    def tunnel_key_for(self, host: str, ssh_port: int) -> bytes | None:
        """The tunnel key this PC already holds for a deployed server, so a re-deploy keeps it."""
        entry = self.servers.find_ssh(host, ssh_port)
        try:
            return self._keys().load_bytes(entry["id"]) if entry else None
        except (OSError, ValueError):
            return None

    def save_deployed(self, name: str, result: dict, private: bytes) -> str:
        """Called when a deployment has finished: remember the server and its tunnel key."""
        from .remote.tunnel import free_port

        entry = self.servers.save_ssh(name, result["host"], result["ssh_port"], result["host_key"], result["remote_port"], free_port())
        self._keys().save(entry["id"], private)
        self._close_tunnels()  # one to this server may still be using the previous host key
        log.info("deployed %s (%s): saved as %s", result["host"], result["kind"], entry["id"])
        return entry["id"]

    def forget_server(self, server_id: str) -> None:
        entry = self.servers.get(server_id)
        self.servers.remove(server_id)
        if entry and entry.get("kind") == "ssh":
            if server_id in self._tunnels:
                self._tunnels.pop(server_id).stop()
            self._keys().delete(server_id)

    def open_deploy(self, *_args) -> None:
        if self._deploy_window is not None:
            try:
                self._deploy_window.show()
                self._deploy_window.restore()
                return
            except Exception:
                self._deploy_window = None
        try:
            from .remote.api import DeployApi
        except ImportError as e:
            system.message_box(f"Deploying to a Linux server is not available in this copy of the app ({e}).")
            return
        api = DeployApi(self)
        window = webview.create_window("Deploy to a Linux server", html=DEPLOY_HTML, width=600, height=640, background_color="#09090b")
        expose_api(window, api)

        def closed() -> None:
            self._deploy_window = None
            api.forget()  # nothing typed in the window outlives it

        window.events.closed += closed
        theme.darken_when_shown(window)
        self._deploy_window = window

    def close_deploy(self) -> None:
        window, self._deploy_window = self._deploy_window, None
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass

    def pick_file(self, _api) -> str | None:
        """A file chooser for the Deploy window (a private key). Only the path comes back."""
        window = self._deploy_window
        if window is None:
            return None
        folder = Path.home() / ".ssh"
        chosen = window.create_file_dialog(webview.OPEN_DIALOG, directory=str(folder) if folder.is_dir() else "")
        return str(chosen[0]) if chosen else None

    def _open(self, entry: dict) -> None:
        self.current = entry
        self.servers.remember(entry["id"])
        log.info("opening %s (%s)", entry["name"], entry["url"])
        self.window.load_url(window_url(entry["url"]))
        try:
            self.window.set_title(f"{TITLE} - {entry['name']}")
        except Exception:
            pass
        self.show()

    def open_servers(self, *_args) -> None:
        if self._servers_window is not None:
            try:
                self._servers_window.show()
                self._servers_window.restore()
                return
            except Exception:
                self._servers_window = None
        window = webview.create_window("Servers", html=SERVERS_HTML, width=560, height=560, background_color="#09090b")
        expose_api(window, ServersApi(self))
        window.events.closed += lambda: setattr(self, "_servers_window", None)
        theme.darken_when_shown(window)
        self._servers_window = window

    def _on_webview_failure(self) -> None:
        # Leave the manager and tray running; only the window is unusable.
        self._no_window = True
        message = (
            "The window could not be shown because the Microsoft WebView2 runtime on this PC is missing or damaged.\n\n"
            f"Install it from {WEBVIEW2_DOWNLOAD} and start Vein Server Manager again."
        )
        if self.url:
            message += (
                "\n\nThe manager is still running. You can also use it from a web browser at "
                f"{self.url} once an admin password has been set."
            )
        # This runs on the window's own thread; a modal box there would freeze it.
        threading.Thread(target=lambda: (self.window.hide(), system.message_box(message, error=False)), daemon=True).start()

    def show(self, *_args) -> None:
        if self._no_window:
            if self.url:
                webbrowser.open(self.url)
            return
        self.window.show()
        self.window.restore()

    def quit(self, *_args) -> None:
        self._quitting = True
        self._close_tunnels()
        for extra in (self._servers_window, self._about_window, self._deploy_window):
            if extra is not None:
                try:
                    extra.destroy()
                except Exception:
                    pass
        self.window.destroy()

    # ---------- run ----------

    def run(self) -> None:
        use_working_webview2()
        logging.getLogger("pywebview").addHandler(_WebViewFailure(self._on_webview_failure))
        width, height = self._saved_size()
        self.current = self.servers.initial()
        # A deployed server needs its SSH tunnel first: show a holding page and connect once the window is up.
        over_ssh = bool(self.current and self.current.get("kind") == "ssh")
        target = {"html": CONNECTING_HTML} if over_ssh else {"url": window_url(self.current["url"])} if self.current else {"html": PLACEHOLDER_HTML}
        first = self.open_servers if self.current is None else (lambda: self.connect(self.current["id"])) if over_ssh else None
        title = f"{TITLE} - {self.current['name']}" if self.current and self.current["id"] != LOCAL_ID else TITLE
        from webview.menu import Menu, MenuAction

        menu = [Menu("Servers", [MenuAction("Manage servers...", self.open_servers), MenuAction("Deploy to a Linux server...", self.open_deploy)])]
        help_items = [MenuAction("Report a problem...", self.report_problem), MenuAction("About Vein Server Manager", self.about)]
        if self.can_update:
            help_items.insert(0, MenuAction("Check for updates...", self.check_for_updates))
        menu.append(Menu("Help", help_items))
        # The menu belongs to this window alone: given to webview.start() instead, every window
        # (Servers, About) would grow the same menu bar.
        # No js_api here on purpose: this window shows pages from managers, possibly remote ones.
        self.window = webview.create_window(title, width=width, height=height, min_size=(900, 600), background_color="#09090b", menu=menu, **target)
        self.window.events.loaded += self._on_loaded
        theme.darken_when_shown(self.window)
        self.window.events.closing += self._on_closing

        item = pystray.MenuItem
        entries = [item("Open", self.show, default=True), item("Servers...", self.open_servers)]
        if self.url:  # actions on this PC's own manager
            entries += [
                item("Open in browser", lambda: webbrowser.open(self.url)),
                pystray.Menu.SEPARATOR,
                item("Start server", lambda: self._server_action("start")),
                item("Restart server", lambda: self._server_action("restart")),
                item("Stop server", lambda: self._server_action("stop")),
            ]
        if self.can_update:
            entries += [pystray.Menu.SEPARATOR, item("Check for updates...", self.check_for_updates)]
        entries += [pystray.Menu.SEPARATOR, item("Quit", self.quit)]
        self.tray = pystray.Icon("vein-server-manager", Image.open(ICON), TITLE, menu=pystray.Menu(*entries))
        self.tray.run_detached()
        if self.single_instance:
            self.single_instance.on_show_request(self.show)

        if self.can_update and self.url and settings.mode() == "service":
            threading.Thread(target=self._watch_update_requests, name="update-requests", daemon=True).start()
        # Backups and the diagnostics zip are downloads; pywebview refuses them unless told otherwise.
        # Each one still goes through a Save As dialog.
        webview.settings["ALLOW_DOWNLOADS"] = True
        install_safe_browser()
        storage = settings.client_dir() / "webview"
        storage.mkdir(parents=True, exist_ok=True)
        try:
            # Not private mode: a sign-in to a remote server should survive closing the window.
            webview.start(first, private_mode=False, storage_path=str(storage))
        finally:
            self.tray.stop()
