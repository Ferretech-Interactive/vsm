"""The key the app uses to reach a deployed server's manager through SSH.

One ed25519 key pair per deployed server, made on this PC. On the server its public half is tied
to an account that can only forward a connection to the manager's port (see `setup_tunnel` in
install.sh), so the private half is worth little by itself; it is still kept encrypted with
Windows DPAPI, which only this Windows user on this PC can undo. The SSH password or key used to
install the server is never stored anywhere.
"""
from __future__ import annotations

import ctypes
import io
import os
import re
import sys
from pathlib import Path

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

_ID = re.compile(r"^[0-9a-f]{6,32}$")


def generate() -> tuple[bytes, str]:
    """A new key pair: (private key in OpenSSH format, public key as 'ssh-ed25519 AAAA...')."""
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, serialization.NoEncryption())
    public = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return private, public.decode("ascii")


def public_line(private: bytes) -> str:
    key = serialization.load_ssh_private_key(private, password=None)
    return key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode("ascii")


def load(private: bytes) -> paramiko.PKey:
    return paramiko.Ed25519Key.from_private_key(io.StringIO(private.decode("ascii")))


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        raise OSError("Keys can only be stored on Windows.")
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    source = _Blob(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    result = _Blob()
    function = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    # 0x1 = CRYPTPROTECT_UI_FORBIDDEN: fail rather than ever show a prompt.
    if not function(ctypes.byref(source), None, None, None, None, 0x1, ctypes.byref(result)):
        raise OSError("Windows could not " + ("protect" if protect else "read") + " the saved key.")
    try:
        return ctypes.string_at(result.pbData, result.cbData)
    finally:
        kernel32.LocalFree(result.pbData)


class KeyStore:
    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, server_id: str) -> Path:
        if not _ID.match(server_id):
            raise ValueError("bad server id")
        return self.folder / f"{server_id}.key"

    def save(self, server_id: str, private: bytes) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self._path(server_id)
        temp = path.with_suffix(".tmp")
        temp.write_bytes(_dpapi(private, protect=True))
        os.replace(temp, path)

    def load_bytes(self, server_id: str) -> bytes:
        return _dpapi(self._path(server_id).read_bytes(), protect=False)

    def load(self, server_id: str) -> paramiko.PKey:
        return load(self.load_bytes(server_id))

    def delete(self, server_id: str) -> None:
        try:
            self._path(server_id).unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
