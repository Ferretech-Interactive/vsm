"""Password-free sign-in for local administrators when the manager runs as a service.

The service cannot share a token file with the desktop window (they run as different
accounts), so it listens on a named pipe instead. A client connects; the service looks
at *who* connected, using Windows' own view of the caller's token, and hands back a
one-time code only to local administrators. The window then trades that code for a
session through the normal loopback-only HTTP route.

pywin32 is imported lazily: only the service and the window on Windows need it.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

log = logging.getLogger("vsm.localauth")

PIPE_NAME = r"\\.\pipe\VeinServerManager"
ADMINISTRATORS = "S-1-5-32-544"
MEDIUM_INTEGRITY = 0x2000
SE_GROUP_ENABLED = 0x4
SE_GROUP_USE_FOR_DENY_ONLY = 0x10
DENIED = b"DENIED"
# Interactive users may connect, read and write, but not create further instances of the pipe
# (GENERIC_WRITE would include that right). SYSTEM and administrators get full control.
#
# A process from a network logon (PowerShell remoting, SSH) carries no Interactive group, so it
# gets in only through the Administrators entry, which Windows applies to elevated tokens only:
# that is how Setup, run remotely by an administrator, applies its choices. Whoever can run an
# elevated program on this PC already controls it. Connections from other machines are a
# different matter and are always refused (PIPE_REJECT_REMOTE_CLIENTS).
PIPE_SDDL = "D:(A;;0x12019b;;;IU)(A;;GA;;;SY)(A;;GA;;;BA)(A;;GA;;;OW)"


def is_local_admin(groups: list[tuple[str, int]], integrity: int, app_container: bool, restricted: bool) -> bool:
    """Decide from a caller's token whether they may manage the server without a password.

    `groups` is (SID string, attributes) for every group in the token. An administrator
    running without elevation still carries the Administrators group, marked deny-only by
    UAC; that counts. A sandboxed process of the same person does too, so low-integrity,
    AppContainer and restricted tokens are refused: a browser tab or a sandboxed app must
    not inherit the right to control the server.
    """
    if app_container or restricted or integrity < MEDIUM_INTEGRITY:
        return False
    return any(sid == ADMINISTRATORS and attrs & (SE_GROUP_ENABLED | SE_GROUP_USE_FOR_DENY_ONLY) for sid, attrs in groups)


def _describe_token(token) -> tuple[list[tuple[str, int]], int, bool, bool]:
    import win32security

    groups = [(win32security.ConvertSidToStringSid(sid), attrs) for sid, attrs in win32security.GetTokenInformation(token, win32security.TokenGroups)]
    label_sid, _ = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
    integrity = label_sid.GetSubAuthority(label_sid.GetSubAuthorityCount() - 1)
    # pywin32 does not expose TokenIsAppContainer (class 29), so ask Windows directly.
    flag, size = ctypes.c_ulong(0), ctypes.c_ulong(0)
    if not ctypes.windll.advapi32.GetTokenInformation(ctypes.c_void_p(int(token)), 29, ctypes.byref(flag), 4, ctypes.byref(size)):
        raise ctypes.WinError()
    app_container = bool(flag.value)
    restricted = bool(win32security.IsTokenRestricted(token))
    return groups, integrity, app_container, restricted


class PipeServer:
    """Hands one-time sign-in codes to local administrators. Runs on its own thread in the service."""

    def __init__(self, issue_code, name: str = PIPE_NAME):
        self._issue_code = issue_code
        self.name = name
        self._stop = threading.Event()
        self._first = True
        # True while Windows still owes us the end of an overlapped operation on the current pipe.
        self._pending = False

    def start(self) -> None:
        threading.Thread(target=self._serve, name="local-auth-pipe", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _create(self):
        import win32file
        import win32pipe
        import win32security

        attributes = win32security.SECURITY_ATTRIBUTES()
        attributes.SECURITY_DESCRIPTOR = win32security.ConvertStringSecurityDescriptorToSecurityDescriptor(
            PIPE_SDDL, win32security.SDDL_REVISION_1
        )
        mode = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
        if self._first:
            mode |= 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE: fail rather than share the name with an impostor
        handle = win32pipe.CreateNamedPipe(
            self.name, mode,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
            win32pipe.PIPE_UNLIMITED_INSTANCES, 256, 256, 2000, attributes,
        )
        self._first = False
        return handle

    def _wait(self, overlapped, milliseconds: int | None) -> bool:
        """Wait for an overlapped operation; `None` waits until the server is stopped."""
        import win32event

        if milliseconds is not None:
            done = win32event.WaitForSingleObject(overlapped.hEvent, milliseconds) == win32event.WAIT_OBJECT_0
            self._pending = not done
            return done
        while not self._stop.is_set():
            if win32event.WaitForSingleObject(overlapped.hEvent, 500) == win32event.WAIT_OBJECT_0:
                self._pending = False
                return True
        return False

    def _started(self, code: int) -> None:
        """Record whether the call just made left an operation running (ERROR_IO_PENDING) or finished at once."""
        import winerror

        self._pending = code == winerror.ERROR_IO_PENDING

    def _serve(self) -> None:
        import pywintypes
        import win32event
        import win32file
        import win32pipe
        import winerror

        while not self._stop.is_set():
            try:
                handle = self._create()
            except pywintypes.error as e:
                log.error("cannot create the sign-in pipe (%s); administrators will need the password", e)
                if self._stop.wait(30):
                    return
                continue
            overlapped = pywintypes.OVERLAPPED()
            overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
            # Windows writes into these buffers, and into `overlapped`, whenever the I/O finishes,
            # which can be after the call that started it has returned. They must stay alive until
            # then: memory released early is corrupted by that late write and the service crashes.
            buffers: list = []
            self._pending = False
            try:
                code = win32pipe.ConnectNamedPipe(handle, overlapped)
                self._started(code)
                if code == winerror.ERROR_IO_PENDING and not self._wait(overlapped, None):
                    return
                self._answer(handle, overlapped, buffers)
            except pywintypes.error as e:
                log.debug("pipe client error: %s", e)
            except Exception:
                log.exception("the sign-in pipe failed to answer a caller")
            finally:
                # Only when something is still running. A call that failed outright (the usual
                # one: the last read, when the client has already hung up) started nothing, and
                # waiting for "it" to finish would hold the pipe for the whole time limit while
                # the next caller is turned away.
                if self._pending:
                    try:
                        win32file.CancelIo(handle)
                    except pywintypes.error:
                        pass
                    self._settle(handle, overlapped)
                try:
                    win32pipe.DisconnectNamedPipe(handle)
                except pywintypes.error:
                    pass
                handle.Close()
                buffers.clear()

    @staticmethod
    def _settle(handle, overlapped, seconds: float = 2.0) -> None:
        """Wait until a cancelled operation has really finished; only then may its memory be released."""
        import pywintypes
        import win32file
        import winerror

        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                win32file.GetOverlappedResult(handle, overlapped, False)
                return
            except pywintypes.error as e:
                if e.winerror != winerror.ERROR_IO_INCOMPLETE:
                    return  # finished, with an error (cancelled, or the client went away)
            time.sleep(0.01)

    def _answer(self, handle, overlapped, buffers: list) -> None:
        import win32api
        import win32con
        import win32event
        import win32file
        import win32security

        # A client must send something before it can be impersonated. The deadline stops a
        # client that connects and then goes silent from blocking everyone else's sign-in.
        win32event.ResetEvent(overlapped.hEvent)
        buffers.append(win32file.AllocateReadBuffer(16))
        self._started(win32file.ReadFile(handle, buffers[-1], overlapped)[0])
        if not self._wait(overlapped, 2000):
            return
        win32security.ImpersonateNamedPipeClient(handle)
        try:
            token = win32security.OpenThreadToken(win32api.GetCurrentThread(), win32con.TOKEN_QUERY, True)
            allowed = is_local_admin(*_describe_token(token))
        finally:
            win32security.RevertToSelf()
        win32event.ResetEvent(overlapped.hEvent)
        buffers.append(self._issue_code().encode("ascii") if allowed else DENIED)
        self._started(win32file.WriteFile(handle, buffers[-1], overlapped)[0])
        if not self._wait(overlapped, 2000):
            return  # the write is still running: it is cancelled and waited for by the caller
        # Disconnecting discards unread data, so give the client a moment to read the reply:
        # this read finishes (with an error) as soon as the client closes its end.
        win32event.ResetEvent(overlapped.hEvent)
        buffers.append(win32file.AllocateReadBuffer(1))
        try:
            self._started(win32file.ReadFile(handle, buffers[-1], overlapped)[0])
        except win32file.error:
            return  # the client has gone already; nothing was started
        self._wait(overlapped, 2000)


def request_code(name: str = PIPE_NAME, expected_server_pid: int | None = None) -> str | None:
    """Ask the service for a sign-in code. Returns None when refused or when there is no service."""
    import pywintypes
    import win32file
    import win32pipe
    import winerror

    # SECURITY_IDENTIFICATION: whoever owns the pipe may see who we are but cannot act as us.
    sqos = 0x00100000 | 0x00010000  # SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION
    handle = None
    for _ in range(15):
        try:
            handle = win32file.CreateFile(
                name, win32file.GENERIC_READ | win32file.GENERIC_WRITE, 0, None, win32file.OPEN_EXISTING, sqos, None
            )
            break
        except pywintypes.error as e:
            # The server answers one caller at a time and reopens the pipe in between, so
            # "busy" or briefly "not there" is worth a short wait; anything else is final.
            if e.winerror not in (winerror.ERROR_PIPE_BUSY, winerror.ERROR_FILE_NOT_FOUND):
                return None
            time.sleep(0.1)
    if handle is None:
        return None
    try:
        if expected_server_pid is not None and win32pipe.GetNamedPipeServerProcessId(handle) != expected_server_pid:
            log.warning("the sign-in pipe is not owned by the service; ignoring it")
            return None
        win32pipe.SetNamedPipeHandleState(handle, win32pipe.PIPE_READMODE_MESSAGE, None, None)
        win32file.WriteFile(handle, b"code")
        _, reply = win32file.ReadFile(handle, 256)
    except pywintypes.error:
        return None
    finally:
        handle.Close()
    return None if reply == DENIED else reply.decode("ascii")
