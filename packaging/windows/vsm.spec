# PyInstaller spec for the Windows desktop app. Build with packaging/windows/build.ps1.
# Produces build/windows/app/VeinServerManager/VeinServerManager.exe (a folder, not a single file:
# single-file builds unpack on every start and trip more antivirus heuristics).
import importlib.util
import os
import re

from PyInstaller.utils.hooks import collect_submodules
from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct, VSVersionInfo,
)

root = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
static = os.path.join(root, "vsm", "static")
if not os.path.exists(os.path.join(static, "index.html")):
    raise SystemExit("vsm/static is missing: build the web UI first (npm run build in ./web).")

# File properties (Properties > Details). build.ps1 passes the same values to the installer.
with open(os.path.join(root, "pyproject.toml"), encoding="utf-8") as f:
    version = re.search(r'^version = "(.+)"', f.read(), re.MULTILINE).group(1)

# What the app uploads when it deploys to a Linux server (vsm/desktop/remote/deploy.py): the Linux
# package of this same version, built by build.ps1 just before this, and the files install.sh needs.
wheel = os.path.join(root, "build", "windows", "work", "wheel", f"vein_server_manager-{version}-py3-none-any.whl")
if not os.path.exists(wheel):
    raise SystemExit("The Linux package is missing: build.ps1 builds it before the exe.")
remote = [(wheel, "remote")] + [(os.path.join(root, *path.split("/")), "remote") for path in (
    "install.sh", "deploy/vsm.service", "deploy/remote/Dockerfile", "docker-compose.yml", "docker/entrypoint.sh")]
numeric = tuple(int(n) for n in re.match(r"(\d+)\.(\d+)\.(\d+)", version).groups()) + (0,)
copyright_text = os.environ.get("VSM_COPYRIGHT", "Copyright (c) FerreTech Interactive")
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=numeric, prodvers=numeric),
    kids=[
        StringFileInfo([StringTable("040904B0", [
            StringStruct("CompanyName", "FerreTech Interactive"),
            StringStruct("FileDescription", "Vein Server Manager"),
            StringStruct("FileVersion", ".".join(map(str, numeric))),
            StringStruct("InternalName", "VeinServerManager"),
            StringStruct("LegalCopyright", copyright_text),
            StringStruct("OriginalFilename", "VeinServerManager.exe"),
            StringStruct("ProductName", "Vein Server Manager"),
            StringStruct("ProductVersion", version),
        ])]),
        VarFileInfo([VarStruct("Translation", [0x0409, 1200])]),
    ],
)

# pywin32 pieces used by the Windows service, the administrator sign-in pipe and the installer helpers.
PYWIN32 = ["win32timezone", "servicemanager", "win32serviceutil", "win32service", "win32event", "win32pipe",
           "win32file", "win32security", "win32api", "win32con", "pywintypes", "winerror"]
hidden = collect_submodules("uvicorn") + collect_submodules("vsm") + ["pystray._win32", "paramiko"] + PYWIN32
if importlib.util.find_spec("discord"):  # optional Discord bot
    hidden += collect_submodules("discord")

a = Analysis(
    [os.path.join(SPECPATH, "launcher.py")],
    pathex=[root],
    datas=[(static, "vsm/static"), (os.path.join(root, "vsm", "desktop", "icon.png"), "vsm/desktop")] + remote,
    hiddenimports=hidden,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    exclude_binaries=True,
    name="VeinServerManager",
    icon=os.path.join(SPECPATH, "vsm.ico"),
    version=version_info,
    console=False,
)
COLLECT(exe, a.binaries, a.datas, name="VeinServerManager")
