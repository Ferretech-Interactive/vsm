"""A folder of .pak mods (~mods, or LogicMods for blueprint mods): upload, enable/disable, delete."""
from __future__ import annotations

import os
from pathlib import Path

from .. import VsmError
from ..core.paths import child, safe_name

MAX_MOD_BYTES = 8 * 1024**3
# IoStore mods ship as a .pak plus matching .utoc/.ucas; the set is handled as one mod.
COMPANIONS = (".utoc", ".ucas")
DISABLED = ".disabled"


class Mods:
    def __init__(self, directory: Path):
        self.dir = directory

    def _companions(self, pak: Path) -> list[Path]:
        stem = pak.name[: -len(DISABLED)] if pak.name.lower().endswith(DISABLED) else pak.name
        stem = stem[: -len(".pak")]
        found = []
        for ext in COMPANIONS:
            for candidate in (pak.with_name(stem + ext), pak.with_name(stem + ext + DISABLED)):
                if candidate.is_file():
                    found.append(candidate)
        return found

    def list(self) -> list[dict]:
        if not self.dir.is_dir():
            return []
        out = []
        for p in sorted(self.dir.iterdir(), key=lambda p: p.name.lower()):
            lower = p.name.lower()
            if p.is_file() and (lower.endswith(".pak") or lower.endswith(".pak" + DISABLED)):
                size = p.stat().st_size + sum(c.stat().st_size for c in self._companions(p))
                out.append({"name": p.name, "enabled": lower.endswith(".pak"), "size": size})
        return out

    def _existing(self, name: str) -> Path:
        p = child(self.dir, name)
        if not p.is_file() or not p.name.lower().endswith((".pak", ".pak" + DISABLED)):
            raise VsmError("Mod not found.", 404)
        return p

    def install(self, filename: str, stream) -> str:
        name = safe_name(os.path.basename(filename or ""))
        if not name.lower().endswith((".pak", *COMPANIONS)):
            raise VsmError("Only .pak files (and their .utoc/.ucas companions) can be installed.")
        self.dir.mkdir(parents=True, exist_ok=True)
        target = child(self.dir, name)
        tmp = target.with_name(target.name + ".part")
        written = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := stream.read(1024 * 1024):
                    written += len(chunk)
                    if written > MAX_MOD_BYTES:
                        raise VsmError("File is too large.", 413)
                    out.write(chunk)
            os.replace(tmp, target)
        finally:
            tmp.unlink(missing_ok=True)
        return name

    def toggle(self, name: str) -> str:
        pak = self._existing(name)
        disable = not pak.name.lower().endswith(DISABLED)
        for p in (pak, *self._companions(pak)):
            is_disabled = p.name.lower().endswith(DISABLED)
            if disable and not is_disabled:
                p.rename(p.with_name(p.name + DISABLED))
            elif not disable and is_disabled:
                p.rename(p.with_name(p.name[: -len(DISABLED)]))
        return pak.name + DISABLED if disable else pak.name[: -len(DISABLED)]

    def delete(self, name: str) -> None:
        pak = self._existing(name)
        for p in (pak, *self._companions(pak)):
            p.unlink()
