"""REST + WebSocket API consumed by the web UI (and usable for automation with VSM_API_TOKEN)."""
from __future__ import annotations

import asyncio
import os
import platform
import subprocess
import time

import psutil
from fastapi import APIRouter, Body, Depends, File, HTTPException, Request, Response, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .. import VsmError, __version__
from .audit import auditor, changes
from .auth import COOKIE, SESSION_TTL, same_origin
from ..core.config import SECRET_FIELDS
from ..core import selfupdate, support
from ..core.network import local_ip, public_ip
from ..server import upnp
from ..game.settings import steam_ids
from ..game.gameplay import schema
from ..server import troubleshoot
from ..manager import Manager
from ..core.paths import IS_WINDOWS


CONFIG_SECRETS = tuple(".".join(field) for field in SECRET_FIELDS)


def manager(request: Request) -> Manager:
    return request.app.state.manager


def require_auth(request: Request) -> None:
    if not request.app.state.manager.auth.authenticated(request):
        raise HTTPException(401, "Not authenticated.")


def require_offline(m: Manager) -> None:
    """The server keeps its ini in memory and rewrites it on exit, so edits while it runs are lost."""
    if m.server.running():
        raise VsmError("Stop the server before changing this.", 409)


public = APIRouter(prefix="/api")
router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


# ---------- auth ----------

class Login(BaseModel):
    password: str


class LocalLogin(BaseModel):
    token: str


class PasswordChange(BaseModel):
    current: str = ""
    new: str


def _set_session(request: Request, response: Response, token: str) -> None:
    secure = request.url.scheme == "https" or os.environ.get("VSM_SECURE_COOKIES", "").lower() in ("1", "true", "yes")
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="strict", secure=secure)


@public.get("/health")
def health():
    return {"ok": True}


@public.get("/auth/state")
def auth_state(request: Request, m: Manager = Depends(manager)):
    authenticated = m.auth.authenticated(request)
    return {
        "authenticated": authenticated,
        "version": __version__,
        "local": m.auth.is_local(request),  # signed in from the desktop app on this machine
        "needs_password": authenticated and m.auth.needs_password,
        # An installed Windows host: lets the sign-in page say where its password came from.
        "service_mode": m.service_mode,
    }


@public.post("/auth/login")
def login(body: Login, request: Request, response: Response, m: Manager = Depends(manager), audit=Depends(auditor)):
    try:
        token = m.auth.login(request.client.host if request.client else "?", body.password)
    except VsmError as e:
        audit("Sign-in refused: too many failed attempts" if e.status == 429 else "Failed sign-in: wrong password")
        raise
    _set_session(request, response, token)
    audit("Signed in with the admin password")
    return {"ok": True}


@public.post("/auth/local")
def local_login(body: LocalLogin, request: Request, response: Response, m: Manager = Depends(manager)):
    try:
        token = m.auth.local_login(request, body.token)
    except VsmError:
        m.audit("Failed desktop sign-in", request.client.host if request.client else "an unknown address")
        raise
    _set_session(request, response, token)
    m.audit("Signed in", "the desktop window on this PC")
    return {"ok": True}


@public.post("/auth/logout")
def logout(request: Request, response: Response, m: Manager = Depends(manager), audit=Depends(auditor)):
    if m.auth.authenticated(request):
        audit("Signed out")
    m.auth.logout(request.cookies.get(COOKIE))
    response.delete_cookie(COOKIE)
    return {"ok": True}


@router.post("/auth/password")
def change_password(body: PasswordChange, request: Request, m: Manager = Depends(manager), audit=Depends(auditor)):
    try:
        m.auth.change_password(body.current, body.new, request.cookies.get(COOKIE))
    except VsmError as e:
        audit(f"Admin password change refused: {e}")
        raise
    audit("Changed the admin password; every other session was signed out")
    return {"ok": True}


# ---------- server control ----------

@router.get("/status")
def status(m: Manager = Depends(manager)):
    return m.server.status()


@router.get("/usage")
def usage(m: Manager = Depends(manager)):
    """The last few minutes of CPU, memory and network samples, for the dashboard graphs."""
    return m.server.usage.history()


@router.post("/server/{action}")
def server_action(action: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    actions = {"start": m.server.request_start, "stop": m.server.request_stop, "restart": m.server.request_restart}
    if action not in actions:
        raise HTTPException(404)
    actions[action]("USER")
    audit(f"Asked the game server to {action}")
    return {"ok": True}


@router.get("/network")
def network(m: Manager = Depends(manager)):
    launch = m.config.get().launch
    return {"internal_ip": local_ip(), "external_ip": public_ip(), "game_port": launch.port}


class UpdateRequest(BaseModel):
    validate_files: bool = False


@router.post("/steam/update")
def steam_update(body: UpdateRequest, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.server.request_update(body.validate_files)
    audit("Started a server " + ("file validation" if body.validate_files else "install/update"))
    return {"ok": True}


@router.get("/steam/branches")
def steam_branches(m: Manager = Depends(manager)):
    m.branches.refresh_if_stale()
    return m.branches.status()


@router.post("/steam/branches/refresh")
def steam_branches_refresh(m: Manager = Depends(manager)):
    m.branches.refresh()
    return m.branches.status()


class SetupPlan(BaseModel):
    admin_password: str = ""  # only where no installer could ask (portable, Linux, Docker)
    config: dict = {}  # same shape as PUT /config
    game: dict = {}  # same shape as PUT /game-settings
    ue4ss: bool = False
    start: bool = True


@router.post("/setup")
def guided_setup(plan: SetupPlan, request: Request, m: Manager = Depends(manager), audit=Depends(auditor)):
    """Validate every choice first, then hand the whole job to the controller."""
    # Checked before anything is saved: the running server would overwrite Game.ini on exit,
    # and the download step could not run beside it.
    if m.server.running():
        raise VsmError("Stop the server before running setup.", 409)
    allowed = {"launch", "network", "behavior", "backups"}
    if set(plan.config) - allowed:
        raise VsmError("Setup can only change launch, network, behaviour and backup settings.")
    m.config.check(plan.config)
    game = m.game.validate(plan.game)
    wants_remote = plan.config.get("network", {}).get("remote_access") is True
    if wants_remote and m.auth.needs_password and not plan.admin_password:
        raise VsmError("Set an admin password before allowing other devices to connect.")
    if plan.admin_password and m.auth.needs_password:
        m.auth.change_password("", plan.admin_password, request.cookies.get(COOKIE), first_time=True)
        audit("Set the admin password during guided setup")
    elif plan.admin_password and not m.auth.verify(plan.admin_password):
        # The same password again is a retry of a setup that already stored it; anything else is a change.
        raise VsmError("An admin password is already set; change it under System.")
    m.server.request_setup({"config": plan.config, "game": game, "ue4ss": plan.ue4ss, "start": plan.start})
    audit("Started the guided setup")
    return {"ok": True}


@router.get("/logs")
def logs(m: Manager = Depends(manager)):
    return {"log": m.hub.snapshot("log"), "steam": m.hub.snapshot("steam"), "events": m.hub.snapshot("event")}


# ---------- configuration ----------

@router.get("/config")
def get_config(m: Manager = Depends(manager)):
    return m.config.public()


@router.put("/config")
def put_config(patch: dict = Body(...), m: Manager = Depends(manager), audit=Depends(auditor)):
    before = m.config.get()
    wants_remote = isinstance(patch.get("network"), dict) and patch["network"].get("remote_access") is True
    if wants_remote and not before.network.remote_access and m.auth.needs_password:
        raise VsmError("Set an admin password before allowing other devices to connect.")
    after = m.config.update(patch)
    if changed := changes(before.model_dump(), after.model_dump(), CONFIG_SECRETS):
        audit(f"Changed manager settings: {changed}")
    if (after.launch.port, after.launch.query_port, after.network.remote_access) != (
        before.launch.port, before.launch.query_port, before.network.remote_access
    ):
        m.request_firewall_sync()
    return m.config.public()


class PortCheckRequest(BaseModel):
    port: int = Field(ge=1024, le=65535)
    query_port: int = Field(ge=1024, le=65535)
    upnp: bool = False
    upnp_adapter: str | None = Field(None, max_length=100)  # the guided setup's choice, not saved yet


@router.post("/portcheck")
def port_check(body: PortCheckRequest, m: Manager = Depends(manager), audit=Depends(auditor)):
    audit(f"Ran the outside port check on {body.port} and {body.query_port}")
    """Ask the Port Probe Service to test the game ports from the internet. Only on request:
    it tells that service this machine's public address and the two port numbers."""
    return m.portcheck.run(body.port, body.query_port, body.upnp, body.upnp_adapter)


@router.post("/manager/restart")
def restart_manager(m: Manager = Depends(manager), audit=Depends(auditor)):
    """Restart the manager itself (not the game server), e.g. to apply the remote-access setting."""
    if not m.restart_hook:
        raise VsmError("Restarting from here is not available in this mode. Restart the manager yourself to apply the change.", 409)
    audit("Asked the manager to restart")
    m.restart_hook()
    return {"ok": True}


@router.get("/game-settings")
def get_game_settings(m: Manager = Depends(manager)):
    return m.game.read()


@router.put("/game-settings")
def put_game_settings(data: dict = Body(...), m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    before = m.game.read()
    m.game.write(data)
    after = m.game.read()
    if changed := changes(before, after, ("password", "discord_chat_webhook", "discord_admin_webhook")):
        audit(f"Changed server settings: {changed}")
    return after


@router.get("/gameplay")
def get_gameplay(m: Manager = Depends(manager)):
    return {"schema": schema(), "values": m.game.read_gameplay()}


@router.put("/gameplay")
def put_gameplay(values: dict = Body(...), m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    before = m.game.read_gameplay()
    m.game.write_gameplay(values)
    after = m.game.read_gameplay()
    if changed := changes(before, after):
        audit(f"Changed gameplay settings: {changed}")
    return after


# ---------- pak mods ----------

def _pak_mod_routes(prefix: str, store, needs_ue4ss: bool = False) -> None:
    """The same four routes for each folder of .pak mods."""
    kind = "blueprint mod" if needs_ue4ss else "mod"

    def guard(m: Manager, write: bool = True) -> None:
        if needs_ue4ss:
            m.ue4ss.require_supported()
        if write:
            require_offline(m)

    @router.get(prefix, name=f"list{prefix}")
    def list_mods(m: Manager = Depends(manager)):
        guard(m, write=False)
        return store(m).list()

    @router.post(prefix, name=f"upload{prefix}")
    def upload_mod(file: UploadFile = File(...), m: Manager = Depends(manager), audit=Depends(auditor)):
        guard(m)
        name = store(m).install(file.filename or "", file.file)
        audit(f"Installed {kind} {name}")
        return {"name": name}

    @router.post(prefix + "/{name}/toggle", name=f"toggle{prefix}")
    def toggle_mod(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
        guard(m)
        renamed = store(m).toggle(name)
        audit(f"Switched {kind} {name} on or off (now {renamed})")
        return {"name": renamed}

    @router.delete(prefix + "/{name}", name=f"delete{prefix}")
    def delete_mod(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
        guard(m)
        store(m).delete(name)
        audit(f"Deleted {kind} {name}")
        return {"ok": True}


_pak_mod_routes("/mods", lambda m: m.mods)
_pak_mod_routes("/logic-mods", lambda m: m.logic_mods, needs_ue4ss=True)


# ---------- UE4SS (script mods, Windows server build only) ----------

class Enabled(BaseModel):
    enabled: bool


class ScriptMod(BaseModel):
    name: str
    enabled: bool


@router.get("/ue4ss")
def ue4ss_status(m: Manager = Depends(manager)):
    return m.ue4ss.status()


@router.post("/ue4ss/install")
def ue4ss_install(m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit("Installed or updated UE4SS from GitHub")
    return m.ue4ss.install_from_github()


@router.post("/ue4ss/upload")
def ue4ss_upload(file: UploadFile = File(...), m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit(f"Installed UE4SS from an uploaded file ({file.filename or 'unnamed'})")
    return m.ue4ss.install_from_upload(file.filename or "", file.file)


@router.post("/ue4ss/enabled")
def ue4ss_enabled(body: Enabled, m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit("Enabled UE4SS" if body.enabled else "Disabled UE4SS (safe mode)")
    return m.ue4ss.set_enabled(body.enabled)


@router.delete("/ue4ss")
def ue4ss_remove(m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit("Removed UE4SS")
    return m.ue4ss.remove()


@router.put("/ue4ss/mods")
def ue4ss_set_mods(mods: list[ScriptMod], m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit("Changed which script mods are enabled, or their order")
    return m.ue4ss.set_mods([mod.model_dump() for mod in mods])


@router.post("/ue4ss/mods")
def ue4ss_upload_mod(file: UploadFile = File(...), m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    installed = m.ue4ss.upload_mod(file.filename or "", file.file)
    audit(f"Installed script mod(s): {', '.join(installed)}")
    return {"installed": installed, "mods": m.ue4ss.mods()}


@router.delete("/ue4ss/mods/{name}")
def ue4ss_delete_mod(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    audit(f"Deleted script mod {name}")
    return m.ue4ss.delete_mod(name)


@router.get("/ue4ss/log")
def ue4ss_log(m: Manager = Depends(manager)):
    return m.ue4ss.log_tail()


# ---------- ini editor ----------

class SectionBody(BaseModel):
    section: str
    entries: list[tuple[str, str]]


@router.get("/ini/sections")
def ini_sections(m: Manager = Depends(manager)):
    return m.game.editable_sections()


@router.get("/ini/section")
def ini_section(name: str, m: Manager = Depends(manager)):
    return m.game.read_section(name)


@router.put("/ini/section")
def put_ini_section(body: SectionBody, m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    m.game.write_section(body.section, body.entries)
    audit(f"Edited ini section [{body.section.strip()}] ({len(body.entries)} entries)")
    return {"ok": True}


# ---------- players ----------

class SteamId(BaseModel):
    steam_id: str


@router.get("/players")
def players(m: Manager = Depends(manager)):
    return {"history": m.history.list(), "bans": m.game.bans()}


class SteamIds(BaseModel):
    ids: list[str] = []


@router.get("/steam/users/lookup")
def steam_user_lookup(q: str, m: Manager = Depends(manager)):
    return m.steam_users.lookup(q[:300])


@router.post("/steam/users")
def steam_user_names(body: SteamIds, m: Manager = Depends(manager)):
    return m.steam_users.names(body.ids)


@router.post("/bans")
def ban(body: SteamId, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.game.set_bans(m.game.bans() + steam_ids([body.steam_id]), running=m.server.running())
    audit(f"Banned {body.steam_id}")
    return m.game.bans()


@router.delete("/bans/{steam_id}")
def unban(steam_id: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.game.set_bans([b for b in m.game.bans() if b != steam_id], running=m.server.running())
    audit(f"Unbanned {steam_id}")
    return m.game.bans()


# ---------- backups ----------

@router.get("/backups")
def list_backups(m: Manager = Depends(manager)):
    return m.backups.list()


@router.post("/backups")
def create_backup(m: Manager = Depends(manager), audit=Depends(auditor)):
    name = m.backups.create("MANUAL")
    audit(f"Created backup {name}")
    return {"name": name}


@router.get("/backups/{name}")
def download_backup(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    path = m.backups.path(name)
    audit(f"Downloaded backup {path.name}")
    return FileResponse(path, filename=path.name, media_type="application/zip")


@router.delete("/backups/{name}")
def delete_backup(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.backups.delete(name)
    audit(f"Deleted backup {name}")
    return {"ok": True}


@router.post("/backups/{name}/restore")
def restore_backup(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    m.backups.restore(name)
    audit(f"Restored backup {name}")
    return {"ok": True}


# ---------- profiles ----------

class ProfileName(BaseModel):
    name: str


@router.get("/profiles")
def list_profiles(m: Manager = Depends(manager)):
    return m.profiles.list()


@router.post("/profiles")
def save_profile(body: ProfileName, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.profiles.save(body.name)
    audit(f"Saved profile {body.name}")
    return m.profiles.list()


@router.post("/profiles/{name}/load")
def load_profile(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    require_offline(m)
    m.profiles.load(name)
    audit(f"Loaded profile {name}, replacing the current settings")
    return {"ok": True}


@router.delete("/profiles/{name}")
def delete_profile(name: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    m.profiles.delete(name)
    audit(f"Deleted profile {name}")
    return m.profiles.list()


# ---------- diagnostics ----------

def port_use(port: int, game_pid: int | None, sockets=None) -> dict:
    """Who has UDP `port` open on this machine: {"state": "game" | "other" | "free" | None, "program": name}.

    Only says what is bound locally. "game" means the game server itself holds the port;
    "other" names the program in the way, which would stop the game server from starting.
    None means the operating system would not say.
    """
    try:
        if sockets is None:
            sockets = psutil.net_connections(kind="udp")
        owners = [c.pid for c in sockets if c.laddr and c.laddr.port == port]
    except (psutil.Error, OSError):
        return {"state": None, "program": ""}
    if not owners:
        return {"state": "free", "program": ""}
    if game_pid and game_pid in owners:
        return {"state": "game", "program": ""}
    program = ""
    for pid in owners:
        try:
            program = psutil.Process(pid).name() if pid else ""
        except psutil.Error:
            program = ""
        if program:
            break
    return {"state": "other", "program": program}


UPDATE_REQUEST_TTL = 60


def _desktop_window(request: Request, m: Manager) -> bool:
    """Is this the desktop window on the installed host's own PC? Only it may start an install."""
    return m.service_mode and m.auth.is_local(request)


@router.get("/update")
def update_check(request: Request, m: Manager = Depends(manager)):
    info = dict(m.updates.check())
    # One click only for a stable (signed) release, and only where the desktop app can run Setup.
    info["one_click"] = bool(info["available"] and not info["prerelease"] and _desktop_window(request, m))
    # Linux: the manager can install the release itself, if it was published with a Linux package.
    info["self_update"] = bool(info["available"] and m.self_update.supported and selfupdate.assets(m.updates.release))
    return info


@router.get("/upnp/adapters")
def upnp_adapters(m: Manager = Depends(manager)):
    """The network adapters the router could be on, and what "automatic" currently means."""
    try:
        auto = upnp.looking_at("")
    except upnp.UpnpError:
        auto = {"name": "", "address": None, "gateway": None}
    return {
        "adapters": [{"name": a["name"], "address": a["address"]} for a in upnp.adapters()],
        "automatic": {"name": auto["name"], "address": auto["address"], "gateway": auto["gateway"]},
    }


class UpnpCheck(BaseModel):
    adapter: str = Field("", max_length=100)


@router.post("/upnp/check")
def upnp_check(body: UpnpCheck, m: Manager = Depends(manager)):
    """Look for the router now and say what answered. Reads only: nothing is forwarded."""
    if not m.upnp.available:
        return {"ok": False, "device": "", "double_nat": False, "message": "Not available inside Docker; forward the ports on your router yourself."}
    try:
        gateway = upnp.find_gateway(adapter=body.adapter)
        return {"ok": True, "device": gateway.responder, "double_nat": upnp.is_double_nat(gateway.external_ip()), "message": ""}
    except upnp.UpnpError as e:
        return {"ok": False, "device": "", "double_nat": False, "message": str(e)}


@router.post("/update/self")
def update_self(m: Manager = Depends(manager), audit=Depends(auditor)):
    """Linux: download the newer release, install it beside this one and restart into it.
    The game server is not stopped. Takes no input: what is installed is decided here."""
    version = m.self_update.start()
    audit(f"Started updating the manager to {version}")
    return {"ok": True, "version": version}


@router.post("/update/install")
def request_update_install(request: Request, m: Manager = Depends(manager), audit=Depends(auditor)):
    """Ask the desktop app to install the update. The manager installs nothing itself: the app
    downloads Setup, checks its signature and asks the person at the PC before running it."""
    if not _desktop_window(request, m):
        raise VsmError("Updates are installed from the Vein Server Manager app on the server's own PC.", 403)
    m.update_requested_at = time.time()
    audit("Asked the desktop app to install the update")
    return {"ok": True}


@router.get("/update/install-request")
def take_update_install_request(request: Request, m: Manager = Depends(manager)):
    """Polled by the desktop app. A request is handed out once and goes stale after a minute."""
    if not _desktop_window(request, m):
        raise HTTPException(403)
    requested = time.time() - m.update_requested_at < UPDATE_REQUEST_TTL
    m.update_requested_at = 0.0
    return {"requested": requested}


@router.get("/diagnostics")
def diagnostics(m: Manager = Depends(manager)):
    launch = m.config.get().launch
    disk = psutil.disk_usage(str(m.paths.data))
    return {
        "public_ip": public_ip(),
        "game_port": launch.port,
        "game_port_use": port_use(launch.port, m.server.pid),
        "query_port": launch.query_port,
        "query_port_use": port_use(launch.query_port, m.server.pid),
        "firewall": m.firewall_status(),
        "upnp": m.upnp.status(),
        "game_log": troubleshoot.check(m.paths.game_log, IS_WINDOWS, m.server.running()),
        "disk_free_gb": round(disk.free / 1024**3, 1),
        "disk_total_gb": round(disk.total / 1024**3, 1),
        "data_dir": str(m.paths.data),
        "server_dir": str(m.paths.server),
        "in_container": os.path.exists("/.dockerenv"),
    }


# ---------- report a problem ----------

@router.get("/support")
def support_info(m: Manager = Depends(manager)):
    """What the diagnostics zip would hold, and what the bug report form can be filled in with."""
    fresh = bool(m.desktop_log and time.time() - m.desktop_log[0] < support.DESKTOP_LOG_TTL)
    return {
        "files": support.contents(m, fresh),
        "desktop_log": fresh,
        "version": __version__,
        "install": support.install_kind(m),
        "os": platform.platform(),
    }


def _health(m: Manager):
    """The health check for the zip; a failing one is reported in it rather than losing the zip,
    which is most wanted exactly when something is broken."""
    def run():
        try:
            return diagnostics(m)
        except Exception as e:
            return {"error": str(e)}
    return run


def _zip(name: str, data: bytes) -> Response:
    return Response(data, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/support/bundle")
def support_bundle(anonymize: bool = True, m: Manager = Depends(manager), audit=Depends(auditor)):
    """The zip in one request, for scripts. The page uses the job below to show progress."""
    data = support.bundle(m, anonymize, _health(m))
    audit("Downloaded diagnostics" + ("" if anonymize else " (not anonymized)"))
    return _zip(support.file_name(), data)


class BundleRequest(BaseModel):
    anonymize: bool = True


@router.post("/support/jobs")
def support_job_start(body: BundleRequest, m: Manager = Depends(manager)):
    return {"id": m.support.start(body.anonymize, _health(m))}


@router.get("/support/jobs/{job_id}")
def support_job_status(job_id: str, m: Manager = Depends(manager)):
    status = m.support.status(job_id)
    if status is None:
        raise HTTPException(404, "No such diagnostics; prepare them again.")
    return status


@router.get("/support/jobs/{job_id}/zip")
def support_job_zip(job_id: str, m: Manager = Depends(manager), audit=Depends(auditor)):
    taken = m.support.take(job_id)
    if taken is None:
        raise HTTPException(404, "No such diagnostics; prepare them again.")
    name, data, anonymized = taken
    audit("Downloaded diagnostics" + ("" if anonymized else " (not anonymized)"))
    return _zip(name, data)


class DesktopLog(BaseModel):
    text: str = Field(max_length=support.DESKTOP_LOG_MAX)


@router.post("/support/desktop-log")
def support_desktop_log(body: DesktopLog, request: Request, m: Manager = Depends(manager)):
    """The app hands over this PC's desktop.log (Help > Report a problem), which the service
    cannot read itself. Only the app's own window on this PC may do this."""
    if not m.auth.is_local(request):
        raise HTTPException(403)
    m.desktop_log = (time.time(), body.text)
    return {"ok": True}


# ---------- live updates ----------

@public.websocket("/ws")
async def ws(socket: WebSocket):
    m: Manager = socket.app.state.manager
    if not same_origin(socket) or not m.auth.authenticated(socket):
        await socket.close(code=4401)
        return
    await socket.accept()
    queue: asyncio.Queue = asyncio.Queue(maxsize=2000)
    m.hub.clients.add(queue)
    try:
        await socket.send_json({"type": "status", "data": m.server.status()})
        while True:
            await socket.send_json(await queue.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        m.hub.clients.discard(queue)
