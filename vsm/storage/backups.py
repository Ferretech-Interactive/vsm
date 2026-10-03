"""Zip backups of the server's Saved directory."""
from __future__ import annotations

import os
import re
import threading
import zipfile
from datetime import datetime
from pathlib import Path

from .. import VsmError
from ..core.paths import child, extract_zip

BACKUP_EXCLUDED_DIRS = {"logs", "crashes"}


class Backups:
    def __init__(self, manager):
        self.m = manager
        self.dir: Path = manager.paths.backups
        self._lock = threading.Lock()
        self.last_created = datetime.now()

    def list(self) -> list[dict]:
        files = sorted(self.dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"name": p.name, "size": p.stat().st_size, "created": p.stat().st_mtime} for p in files]

    def path(self, name: str) -> Path:
        p = child(self.dir, name)
        if p.suffix != ".zip" or not p.is_file():
            raise VsmError("Backup not found.", 404)
        return p

    def create(self, reason: str = "MANUAL") -> str:
        saved = self.m.paths.saved
        if not saved.is_dir():
            raise VsmError("Nothing to back up yet: the server has no save data.")
        if not self._lock.acquire(blocking=False):
            raise VsmError("A backup is already running.", 409)
        try:
            cfg = self.m.config.get().backups
            name = re.sub(r'[<>:"/\\|?*]', "_", datetime.now().strftime(cfg.name_format)).strip(" .") or "backup"
            target = self.dir / f"{name}.zip"
            tmp = self.dir / f"{name}.zip.tmp"
            with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
                for root, dirs, files in os.walk(saved):
                    dirs[:] = [d for d in dirs if d.lower() not in BACKUP_EXCLUDED_DIRS]
                    for f in files:
                        if f.lower().endswith(".log"):
                            continue
                        full = Path(root) / f
                        try:
                            z.write(full, full.relative_to(saved))
                        except OSError:
                            pass  # file vanished or is locked mid-save; skip it
            os.replace(tmp, target)
            self.last_created = datetime.now()
            if cfg.retention > 0:
                for old in sorted(self.dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)[:-cfg.retention]:
                    old.unlink(missing_ok=True)
            self.m.event("BACKUP", f"Created {target.name} ({reason}).")
            return target.name
        finally:
            self._lock.release()

    def delete(self, name: str) -> None:
        self.path(name).unlink()

    def restore(self, name: str) -> None:
        """Extract a backup over the Saved directory. The server must be offline."""
        src = self.path(name)
        saved = self.m.paths.saved
        saved.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(src) as z:
            extract_zip(z, [(info, info.filename) for info in z.infolist()], saved)
        self.m.event("BACKUP", f"Restored {name}.")
