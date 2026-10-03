"""Windows integration: the service and its helpers (run elevated by Setup), and small desktop utilities."""
from __future__ import annotations

import ctypes
import hashlib
import struct
import subprocess
import sys
import threading
import time

from .service import ACCOUNT, DESCRIPTION, DISPLAY_NAME, PRESHUTDOWN_MS, SERVICE_NAME
from .settings import FROZEN

TASK_NAME = "VeinServerManager"  # per-user boot task: portable installs and pre-service builds
FIREWALL_TASK = "VeinServerManager Firewall Sync"
ADMINISTRATORS_SID = "S-1-5-32-544"
SYSTEM_SID = "S-1-5-18"

_MUTEX = "Local\\VeinServerManagerDesktop"
_SHOW_EVENT = "Local\\VeinServerManagerShow"
_ERROR_ALREADY_EXISTS = 183


def _powershell(script: str) -> None:
    # Without the explicit exits, powershell.exe reports failure whenever the *last* command
    # had a suppressed, harmless error (such as "no existing task to remove").
    wrapped = "$ErrorActionPreference = 'Stop'; try { " + script + " } catch { [Console]::Error.WriteLine($_); exit 1 }; exit 0"
    result = subprocess.run(
        [_system32(r"WindowsPowerShell\v1.0\powershell.exe"), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", wrapped],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"PowerShell exited with code {result.returncode}")


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _system32(name: str) -> str:
    buffer = ctypes.create_unicode_buffer(260)
    ctypes.windll.kernel32.GetSystemDirectoryW(buffer, 260)
    return buffer.value + "\\" + name


def _run(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(args, capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if check and result.returncode != 0:
        detail = (result.stdout + result.stderr).strip().splitlines()
        tool = args[0].rsplit("\\", 1)[-1]
        raise RuntimeError(f"{tool} {' '.join(args[1:3])} failed: {detail[-1] if detail else result.returncode}")
    return result


# ---------- per-user boot task (portable build) ----------

def register_startup() -> None:
    """Start the manager at boot as the current user, whether or not anyone is logged on."""
    program, arguments = (sys.executable, "--headless") if FROZEN else (sys.executable, "-m vsm.desktop --headless")
    _powershell(
        f"$a = New-ScheduledTaskAction -Execute {_quote(program)} -Argument {_quote(arguments)};"
        "$t = New-ScheduledTaskTrigger -AtStartup;"
        "$s = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3"
        " -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries;"
        # S4U: no stored password, no elevated rights at runtime.
        '$p = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\\$env:USERNAME" -LogonType S4U -RunLevel Limited;'
        f"Register-ScheduledTask -TaskName {_quote(TASK_NAME)} -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null"
    )


def unregister_startup() -> None:
    _powershell(
        f"if (Get-ScheduledTask -TaskName {_quote(TASK_NAME)} -ErrorAction SilentlyContinue) {{"
        f" Stop-ScheduledTask -TaskName {_quote(TASK_NAME)};"
        f" Unregister-ScheduledTask -TaskName {_quote(TASK_NAME)} -Confirm:$false }}"
    )


# ---------- Windows service (installed mode) ----------

def service_sid(name: str) -> str:
    """The SID Windows derives for a service's own account (NT SERVICE\\<name>)."""
    digest = hashlib.sha1(name.upper().encode("utf-16-le")).digest()
    return "S-1-5-80-" + "-".join(str(part) for part in struct.unpack("<5I", digest))


def add_start_right(sddl: str) -> str:
    """Let interactive users start (not stop or reconfigure) the service, so the window can."""
    ace = "(A;;RP;;;IU)"
    if ace in sddl or not sddl.startswith("D:"):
        return sddl
    return "D:" + ace + sddl[2:]


def service_state() -> tuple[str, int]:
    """('running' | 'stopped' | 'starting' | 'stopping' | 'missing', process id)."""
    import pywintypes
    import win32service

    names = {
        win32service.SERVICE_RUNNING: "running", win32service.SERVICE_STOPPED: "stopped",
        win32service.SERVICE_START_PENDING: "starting", win32service.SERVICE_STOP_PENDING: "stopping",
    }
    try:
        manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
        try:
            handle = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_QUERY_STATUS)
            try:
                status = win32service.QueryServiceStatusEx(handle)
            finally:
                win32service.CloseServiceHandle(handle)
        finally:
            win32service.CloseServiceHandle(manager)
    except pywintypes.error:
        return "missing", 0
    return names.get(status["CurrentState"], "stopped"), status["ProcessId"]


def start_service() -> bool:
    import pywintypes
    import win32serviceutil

    try:
        win32serviceutil.StartService(SERVICE_NAME)
        return True
    except pywintypes.error as e:
        return e.winerror == 1056  # already running (Windows restarted it, or another window got there first)


def stop_service(timeout: int = 60) -> None:
    _run([_system32("sc.exe"), "stop", SERVICE_NAME], check=False)
    deadline = time.time() + timeout
    while time.time() < deadline and service_state()[0] not in ("stopped", "missing"):
        time.sleep(1)


def secure_data_folder(data_dir: str) -> None:
    """Give the data folder to the service account, administrators and SYSTEM only."""
    icacls = _system32("icacls.exe")
    sid = service_sid(SERVICE_NAME)
    listing = _run([icacls, data_dir], check=False).stdout
    if ACCOUNT not in listing and sid not in listing:
        # First time under the service: files from an older per-user install are owned by that
        # user, and an owner can always change permissions back. Take ownership and clear the
        # per-file permissions. /L acts on links themselves rather than whatever they point at.
        _run([icacls, data_dir, "/setowner", "*" + ADMINISTRATORS_SID, "/T", "/C", "/L", "/Q"], check=False)
        _run([icacls, data_dir, "/reset", "/T", "/C", "/L", "/Q"], check=False)
    _run([icacls, data_dir, "/inheritance:r", "/grant:r",
          f"*{sid}:(OI)(CI)M", f"*{ADMINISTRATORS_SID}:(OI)(CI)F", f"*{SYSTEM_SID}:(OI)(CI)F"])


def register_firewall_task(exe: str) -> None:
    """A SYSTEM task with one fixed action, which the service account (and nobody else) may run."""
    sddl = f"D:P(A;;FA;;;BA)(A;;FA;;;SY)(A;;GRGX;;;{service_sid(SERVICE_NAME)})"
    _powershell(
        "$s = New-Object -ComObject Schedule.Service; $s.Connect();"
        "$d = $s.NewTask(0);"
        "$d.RegistrationInfo.Description = 'Keeps the Windows Firewall rules for the Vein game server in step with the manager settings.';"
        "$d.Principal.UserId = 'SYSTEM'; $d.Principal.LogonType = 5; $d.Principal.RunLevel = 1;"
        "$d.Settings.Enabled = $true; $d.Settings.MultipleInstances = 2; $d.Settings.ExecutionTimeLimit = 'PT2M';"
        "$d.Settings.DisallowStartIfOnBatteries = $false; $d.Settings.StopIfGoingOnBatteries = $false; $d.Settings.StartWhenAvailable = $true;"
        "$t = $d.Triggers.Create(8);"  # also at boot, so the rule is right before anyone asks
        f"$a = $d.Actions.Create(0); $a.Path = {_quote(exe)}; $a.Arguments = '--sync-firewall';"
        f"$s.GetFolder('\\').RegisterTaskDefinition({_quote(FIREWALL_TASK)}, $d, 6, 'SYSTEM', $null, 5, {_quote(sddl)}) | Out-Null"
    )


def run_firewall_task() -> bool:
    """Ask Windows to run the firewall task now. Works for the service account and administrators."""
    return _run([_system32("schtasks.exe"), "/Run", "/TN", FIREWALL_TASK], check=False).returncode == 0


def install_service(exe: str, data_dir: str) -> None:
    import win32service

    sc = _system32("sc.exe")
    exists = service_state()[0] != "missing"
    _run([sc, "config" if exists else "create", SERVICE_NAME, "binPath=", f'"{exe}" --service',
          "obj=", ACCOUNT, "start=", "auto", "DisplayName=", DISPLAY_NAME])
    _run([sc, "description", SERVICE_NAME, DESCRIPTION])
    _run([sc, "failure", SERVICE_NAME, "reset=", "86400", "actions=", "restart/5000/restart/5000/restart/60000"])
    _run([sc, "sidtype", SERVICE_NAME, "unrestricted"])
    # Service accounts are given SeImpersonatePrivilege by default, a well-known route to SYSTEM
    # for any code running as them (game mods included). Naming the one privilege the service
    # needs removes all the others.
    _run([sc, "privs", SERVICE_NAME, "SeChangeNotifyPrivilege"])
    sddl = _run([sc, "sdshow", SERVICE_NAME]).stdout.strip().splitlines()[-1].strip()
    _run([sc, "sdset", SERVICE_NAME, add_start_right(sddl)])

    manager = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_ALL_ACCESS)
    try:
        handle = win32service.OpenService(manager, SERVICE_NAME, win32service.SERVICE_CHANGE_CONFIG)
        try:
            win32service.ChangeServiceConfig2(handle, win32service.SERVICE_CONFIG_PRESHUTDOWN_INFO, PRESHUTDOWN_MS)
        finally:
            win32service.CloseServiceHandle(handle)
    finally:
        win32service.CloseServiceHandle(manager)

    secure_data_folder(data_dir)
    register_firewall_task(exe)
    try:  # builds before the service ran the manager as the installing user from this task
        unregister_startup()
    except RuntimeError:
        pass


def uninstall_service() -> None:
    from . import firewall

    stop_service()
    _run([_system32("sc.exe"), "delete", SERVICE_NAME], check=False)
    _run([_system32("schtasks.exe"), "/Delete", "/TN", FIREWALL_TASK, "/F"], check=False)
    firewall.remove_all()
    try:
        unregister_startup()
    except RuntimeError:
        pass


# ---------- desktop utilities ----------

class SingleInstance:
    """One desktop window per user session; a second launch just brings the first to the front."""

    def __init__(self) -> None:
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._mutex = self._kernel32.CreateMutexW(None, False, _MUTEX)
        self.already_running = ctypes.get_last_error() == _ERROR_ALREADY_EXISTS
        self._event = self._kernel32.CreateEventW(None, False, False, _SHOW_EVENT)

    def signal_existing(self) -> None:
        self._kernel32.SetEvent(self._event)

    def on_show_request(self, callback) -> None:
        def wait() -> None:
            while self._kernel32.WaitForSingleObject(self._event, 0xFFFFFFFF) == 0:
                callback()

        threading.Thread(target=wait, name="single-instance", daemon=True).start()


def ask(text: str, title: str = "Vein Server Manager") -> bool:
    """A yes/no question in front of everything else. True for Yes."""
    MB_YESNO, MB_ICONQUESTION, MB_SETFOREGROUND, MB_TOPMOST, IDYES = 0x4, 0x20, 0x10000, 0x40000, 6
    return ctypes.windll.user32.MessageBoxW(0, text, title, MB_YESNO | MB_ICONQUESTION | MB_SETFOREGROUND | MB_TOPMOST) == IDYES


def message_box(text: str, title: str = "Vein Server Manager", error: bool = True) -> None:
    ctypes.windll.user32.MessageBoxW(0, text, title, 0x10 if error else 0x30)
